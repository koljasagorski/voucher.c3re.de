from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.utils import timezone

from accounts.models import User
from voucher import services
from voucher.models import Assignment, Event, InboundMail, TicketRequest, Voucher


def test_process_deadlines_command(event, make_request, make_voucher):
    make_request()
    assignment = services.assign_next(make_voucher())
    Assignment.objects.filter(pk=assignment.pk).update(deadline_at=timezone.now() - timedelta(minutes=1))
    out = StringIO()
    call_command("process_deadlines", stdout=out)
    assert "expired=1" in out.getvalue()


def test_purge_old_data(event, make_request, make_voucher):
    now = timezone.now()
    old = Event.objects.create(
        name="40C3", slug="40c3", phase_start=now - timedelta(days=130), phase_end=now - timedelta(days=100)
    )
    old_request = make_request(ev=old)
    old_voucher = make_voucher(ev=old)
    services.assign_to(old_voucher, old_request)
    old_mail = InboundMail.objects.create(
        received_at=now - timedelta(days=120), message_id="<old@x>", from_address="a@b", raw="x",
        parse_status=InboundMail.ParseStatus.NO_CODE,
    )
    current_request = make_request()
    current_voucher = make_voucher()

    call_command("purge_old_data", stdout=StringIO())

    assert not Voucher.objects.filter(pk=old_voucher.pk).exists()
    assert not Assignment.objects.filter(voucher_id=old_voucher.pk).exists()
    assert not TicketRequest.objects.filter(pk=old_request.pk).exists()
    assert not InboundMail.objects.filter(pk=old_mail.pk).exists()
    assert User.objects.filter(pk=old_request.user_id).exists()  # accounts stay
    assert Voucher.objects.filter(pk=current_voucher.pk).exists()
    assert TicketRequest.objects.filter(pk=current_request.pk).exists()
