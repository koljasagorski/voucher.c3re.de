import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from voucher.models import Assignment, Event


def test_only_one_active_event(event):
    with pytest.raises(IntegrityError), transaction.atomic():
        Event.objects.create(
            name="42C3",
            slug="42c3",
            phase_start=event.phase_start,
            phase_end=event.phase_end,
            is_active=True,
        )


def test_one_active_assignment_per_voucher(make_request, make_voucher):
    voucher = make_voucher()
    now = timezone.now()
    Assignment.objects.create(
        voucher=voucher, ticket_request=make_request(), assigned_at=now, deadline_at=now
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        Assignment.objects.create(
            voucher=voucher, ticket_request=make_request(), assigned_at=now, deadline_at=now
        )


def test_voucher_str_hides_code(make_voucher):
    voucher = make_voucher(code="SECRETCODE123456")
    assert "SECRETCODE123456" not in str(voucher)


def test_log_filter_masks_codes():
    from voucher.logging import mask_codes

    text = "see https://tickets.events.ccc.de/x/?voucher=ABCDEF and ABCDEFGH12345678"
    masked = mask_codes(text)
    assert "ABCDEF" not in masked
    assert "ABCDEFGH12345678" not in masked
