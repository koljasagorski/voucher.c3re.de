from django.core.management.base import BaseCommand

from voucher.services import process_deadlines


class Command(BaseCommand):
    help = "Send redeem/pay reminders and expire overdue assignments."

    def handle(self, *args, **options):
        result = process_deadlines()
        self.stdout.write(", ".join(f"{key}={value}" for key, value in result.items()))
