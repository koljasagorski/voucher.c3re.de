from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from tests.test_dashboards import admin_client  # noqa: F401  (fixture)
from voucher import services
from voucher.models import AuditLog, Event, TicketRequest, Voucher

S = TicketRequest.Status


@pytest.fixture
def lottery_event(event):
    event.lottery_at = timezone.now() + timedelta(days=1)
    event.save()
    return event


@pytest.fixture
def reverse_shuffle(monkeypatch):
    """Make the draw deterministic: the 'random' order is the reversed order."""
    monkeypatch.setattr("random.SystemRandom.shuffle", lambda self, items: items.reverse())


def order(event):
    return list(event.requests.order_by("position").values_list("pk", flat=True))


def test_no_assignment_before_the_draw(lottery_event, make_request, make_voucher):
    request = make_request()
    voucher = make_voucher()
    assert services.assign_next(voucher) is None
    voucher.refresh_from_db()
    request.refresh_from_db()
    assert voucher.status == Voucher.Status.RECEIVED
    assert request.status == S.WAITING


def test_draw_shuffles_early_entries_and_keeps_late_ones_behind(
    lottery_event, make_request, make_voucher, reverse_shuffle
):
    a, b, c = make_request(), make_request(), make_request()
    late = make_request()
    TicketRequest.objects.filter(pk=late.pk).update(created_at=lottery_event.lottery_at + timedelta(minutes=1))
    voucher = make_voucher()

    drawn = services.run_lottery(lottery_event, now=lottery_event.lottery_at + timedelta(minutes=5))

    assert [r.pk for r in drawn] == [c.pk, b.pk, a.pk]
    assert order(lottery_event) == [c.pk, b.pk, a.pk, late.pk]
    lottery_event.refresh_from_db()
    assert lottery_event.lottery_done_at is not None
    # Free voucher goes to the first drawn person right after the draw.
    voucher.refresh_from_db()
    assert voucher.assignments.get().ticket_request_id == c.pk
    entry = AuditLog.objects.get(action="lottery")
    assert entry.details["order"] == [c.pk, b.pk, a.pk]

    with pytest.raises(services.InvalidTransition):
        services.run_lottery(lottery_event)


def test_join_after_draw_goes_to_end(lottery_event, make_request, make_user):
    first = make_request()
    services.run_lottery(lottery_event)
    later = services.join_waitlist(lottery_event, make_user())
    assert order(lottery_event) == [first.pk, later.pk]


def test_process_deadlines_draws_when_due(lottery_event, make_request, make_voucher):
    make_request()
    make_voucher()
    assert services.process_deadlines()["lotteries"] == 0  # due tomorrow
    # Use the real clock (a fake future "now" would also expire the fresh assignment).
    Event.objects.filter(pk=lottery_event.pk).update(lottery_at=timezone.now() - timedelta(seconds=1))
    assert services.process_deadlines()["lotteries"] == 1
    assert services.process_deadlines()["lotteries"] == 0
    assert Voucher.objects.get().status == Voucher.Status.ASSIGNED


def test_admin_can_draw_early_and_dashboard_explains(admin_client, lottery_event, make_request):  # noqa: F811
    request = make_request()
    user_client = Client()
    user_client.force_login(request.user)
    assert "ausgelost" in user_client.get(reverse("voucher:dashboard")).content.decode()
    assert "Jetzt auslosen" in admin_client.get(reverse("voucher:admin_waitlist")).content.decode()

    admin_client.post(reverse("voucher:admin_action"), {"op": "lottery", "event_id": lottery_event.pk})
    lottery_event.refresh_from_db()
    assert lottery_event.lottery_done_at is not None
    assert "Vor dir warten" in user_client.get(reverse("voucher:dashboard")).content.decode()
