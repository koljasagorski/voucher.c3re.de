from django.core.management.base import BaseCommand

from voucher.notifications import retry_queued


class Command(BaseCommand):
    help = "Retry outgoing mails that could not be sent yet."

    def handle(self, *args, **options):
        result = retry_queued()
        self.stdout.write(", ".join(f"{key}={value}" for key, value in result.items()))
