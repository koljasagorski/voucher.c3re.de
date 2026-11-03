from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from imap_tools import AND, MailBox, MailMessageFlags

from voucher.mail_in import process_raw_mail


class Command(BaseCommand):
    help = "Fetch unread mails from the IMAP inbox, store and process them, mark them as read."

    def handle(self, *args, **options):
        if not (settings.IMAP_HOST and settings.IMAP_USER):
            raise CommandError("IMAP_HOST and IMAP_USER must be configured.")
        processed = 0
        with MailBox(settings.IMAP_HOST).login(
            settings.IMAP_USER, settings.IMAP_PASSWORD, initial_folder=settings.IMAP_FOLDER
        ) as mailbox:
            for message in mailbox.fetch(AND(seen=False), mark_seen=False, bulk=True):
                process_raw_mail(message.obj.as_bytes())
                # Mark as read only after the mail is safely stored.
                mailbox.flag(message.uid, MailMessageFlags.SEEN, True)
                processed += 1
        self.stdout.write(f"{processed} mail(s) processed")
