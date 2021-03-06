import os
import pygmail.errors
from .fs import sanatize_filename, unique_filename
from pygmail.account import Account

ATTACHMENT_MIMES = ('image/jpeg', 'image/png', 'image/gif')

class GmailImageExtractor(object):
    """Image extrating class which handles connecting to gmail on behalf of
    a user over IMAP, extracts images from messages in a Gmail account,
    writes them to disk, allows users to delete extracted images, and then
    syncronizes the messages in the gmail account by deleting any images
    deleted from the file system from their corresponding message in the
    account.
    """

    def __init__(self, dest, email, password, limit=None, batch=10, replace=False):
        """
        Args:
            dest     -- the path on the file system where images should be
                        extracted and written to.
            email -- the username of the Gmail account to connect to
            password -- the password of the Gmail account to connect to

        Keyword Args:
            limit   -- an optional limit of the total number of messages to
                       download from the gmail account.
            batch   -- the maximum number of messages to download from Gmail
                       at the same time.
            replace -- whether to rewrite the messages in the Gmail account
                       in place (True) or to just write a second, parallel
                       copy of the altered message and leave the original
                       version alone.

        raise:
            ValueError -- If the given dest path to write extracted images to
                          is not writeable.
        """
        self.dest = dest

        if not self.validate_path():
            raise ValueError("{0} is not a writeable directory".format(dest))

        self.limit = limit
        self.batch = batch
        self.replace = replace
        self.email = email
        self.password = password

    def validate_path(self):
        """Checks to see the currently selected destiation path, for where
        extracted images should be written, is a valid path that we can
        read and write from.

        Return:
            A boolean description of whether the currently selected destination
            is a valid path we can read from and write to.
        """
        if not os.path.isdir(self.dest):
            return False
        elif not os.access(self.dest, os.W_OK):
            return False
        else:
            return True

    def connect(self):
        """Attempts to connect to Gmail using the username and password provided
        at instantiation.

        Returns:
            Returns a boolean description of whether we were able to connect
            to Gmail using the current parameters.
        """
        mail = Account(self.email, password=self.password)
        trash_folder = mail.trash_mailbox()
        if pygmail.errors.is_error(trash_folder):
            return False
        else:
            self.mail = mail
            self.trash_folder = trash_folder
            self.inbox = mail.all_mailbox()
            return True

    def num_messages_with_attachments(self):
        """Checks to see how many Gmail messages have attachments in the
        currently connected gmail account.

        This should only be called after having succesfully connected to Gmail.

        Return:
            The number of messages in the Gmail account that have at least one
            attachment (as advertised by Gmail).
        """
        limit = self.limit if self.limit > 0 else False
        gm_ids = self.inbox.search("has:attachment", gm_ids=True, limit=limit)
        return len(gm_ids)

    def extract(self, callback=None):
        """Extracts images from Gmail messages and writes them to the
        path set at instantiation.

        Keyword Args:
            callback -- An optional funciton that will be called with updates
                        about the image extraction process. If provided,
                        will be called with either the following arguments

                        ('image', attachment_name, disk_name)
                        when writing a message to disk, where
                        `attachment_name` is the name of the attachment
                        as advertised in the Email message, and `disk_name`
                        is the name of the file as written to disk.

                        ('message', first)
                        when fetching messages from Gmail, where `first` is the
                        index of the current message being downloaded.

        Returns:
            The number of images written to disk.
        """
        def _cb(*args):
            if callback:
                callback(*args)

        attachment_count = 0
        num_messages = 0
        offset = 0
        per_page = min(self.batch, self.limit) if self.limit else self.batch
        # Keep track of which attachments belong to which messages.  Do this
        # by keeping track of all attachments downloaded to the filesystem
        # (used as the dict key) and pairing it with two values, the gmail
        # message id and the hash of the attachment (so that we can uniquely
        # identify the attachment again)
        self.mapping = {}
        hit_limit = False
        while True and not hit_limit:
            _cb('message', offset + 1)
            messages = self.inbox.search("has:attachment", full=True,
                                         limit=per_page, offset=offset)
            if len(messages) == 0:
                break
            for msg in messages:
                for att in msg.attachments():
                    if att.type in ATTACHMENT_MIMES:
                        poss_fname = u"{0} - {1}".format(msg.subject, att.name())
                        safe_fname = sanatize_filename(poss_fname)
                        fname = unique_filename(self.dest, safe_fname)

                        _cb('image', att.name(), fname)
                        h = open(os.path.join(self.dest, fname), 'w')
                        h.write(att.body())
                        h.close()

                        self.mapping[fname] = msg.gmail_id, att.sha1(), msg.subject
                        attachment_count += 1
                num_messages += 1
                if self.limit > 0 and num_messages >= self.limit:
                    hit_limit = True
                    break
            offset += per_page
        return attachment_count

    def check_deletions(self):
        """Checks the filesystem to see which image attachments, downloaded
        in the self.extract() step, have been removed since extraction, and
        thus should be removed from Gmail.

        Returns:
            The number of attachments that have been deleted from the
            filesystem.
        """
        # Now we can find the attachments the user wants removed from their
        # gmail account by finding every file in the mapping that is not
        # still on the file system
        #
        # Here we want to group attachments by gmail_id, so that we only act on
        # a single email message once, instead of pulling it down multiple times
        # (which would change its gmail_id and ruin all things)
        self.to_delete = {}
        self.to_delete_subjects = {}
        self.num_deletions = 0
        for a_name, (gmail_id, a_hash, msg_subject) in self.mapping.items():
            if not os.path.isfile(os.path.join(self.dest, a_name)):
                if not gmail_id in self.to_delete:
                    self.to_delete[gmail_id] = []
                    self.to_delete_subjects[gmail_id] = msg_subject
                self.to_delete[gmail_id].append(a_hash)
                self.num_deletions += 1
        return self.num_deletions

    def sync(self, label='"Images redacted"', callback=None):
        """Finds image attachments that were downloaded during the
        self.extract() step, and deletes any attachments that were deleted
        from disk from their corresponding images in Gmail.

        Keyword Args:
            label    -- Gmail label to use either as a temporary work label
                        (if instatiated with replace=True) or where the altered
                        images will be stored (if instatiated with
                        replace=False). Note that this label should be in valid
                        ATOM string format.
            callback -- An optional funciton that will be called with updates
                        about the message update process. If provided,
                        will be called with the following sets of arguments:

                        ('fetch', subject, num_attach)
                        Called before fetching a message from gmail. `subject`
                        is the subject of the email message to download, and
                        `num_attach` is the number of attachments to be removed
                        from that message.

                        ('write', subject)
                        Called before writing the altered version of the message
                        back to Gmail.

        Returns:
            Two values, first being the number of attachments that were removed
            from messages in Gmail, and second is the number of messages that
            were altered.
        """
        try:
            num_to_delete = self.num_deletions
        except AttributeError:
            num_to_delete = self.check_deletions()

        def _cb(*args):
            if callback:
                callback(*args)

        num_msg_changed = 0
        num_attch_removed = 0
        for gmail_id, attch_to_remove in self.to_delete.items():
            msg_sbj = self.to_delete_subjects[gmail_id]

            _cb('fetch', msg_sbj, len(attch_to_remove))
            msg_to_change = self.inbox.fetch_gm_id(gmail_id, full=True)
            attach_hashes = {a.sha1(): a for a in msg_to_change.attachments()}
            removed_attachments = 0
            for attachment_hash in attch_to_remove:
                attach_to_delete = attach_hashes[attachment_hash]
                if attach_to_delete.remove():
                    removed_attachments += 1
                    num_attch_removed += 1

            if removed_attachments:
                num_msg_changed += 1
                _cb('write', msg_sbj)
                if self.replace:
                    msg_to_change.save(self.trash_folder.name, safe_label=label)
                else:
                    msg_to_change.save_copy(label)
        return num_attch_removed, num_msg_changed


