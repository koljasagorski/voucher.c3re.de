from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from voucher.models import Event, InboundMail, OutgoingMail, TicketRequest, Voucher
from voucher.services import audit


class Command(BaseCommand):
    help = (
        "Delete vouchers, assignments, inbound mails and ticket requests of events whose "
        "phase ended more than PURGE_AFTER_DAYS (90) days ago. Accounts are kept."
    )

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=settings.PURGE_AFTER_DAYS)
        events = list(Event.objects.filter(phase_end__lt=cutoff))
        with transaction.atomic():
            # Mails of those events' vouchers, plus any mail older than the retention period.
            _, mails = InboundMail.objects.filter(
                Q(voucher__event__in=events) | Q(received_at__lt=cutoff)
            ).delete()
            _, vouchers = Voucher.objects.filter(event__in=events).delete()  # cascades to assignments
            _, requests = TicketRequest.objects.filter(event__in=events).delete()
            # Mails that never went out (may contain codes) are not kept forever either.
            OutgoingMail.objects.filter(created_at__lt=cutoff).delete()
            for event in events:
                audit("purge", event)
        self.stdout.write(
            f"events={len(events)} "
            f"mails={mails.get('voucher.InboundMail', 0)} "
            f"vouchers={vouchers.get('voucher.Voucher', 0)} "
            f"assignments={vouchers.get('voucher.Assignment', 0) + requests.get('voucher.Assignment', 0)} "
            f"requests={requests.get('voucher.TicketRequest', 0)}"
        )
