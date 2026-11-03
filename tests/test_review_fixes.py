"""Regression tests for findings of the independent review."""

from datetime import timedelta
from datetime import timezone as dt_timezone

import pytest
from django.core.cache import caches
from django.core.management import call_command
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from accounts import ratelimit
from voucher import services
from voucher.models import Assignment, AuditLog, TicketRequest, Voucher

S = TicketRequest.Status

DB_CACHE = {"default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "rl_test_cache"}}


@pytest.mark.django_db
@override_settings(CACHES=DB_CACHE)
def test_rate_limit_window_survives_db_cache():
    """The DB cache's incr() would reset the timeout to 300 s; the window must stay 1 h."""
    call_command("createcachetable")
    caches["default"].clear()
    assert all(ratelimit.hit("register:ip", "203.0.113.7", 5, 3600) for _ in range(5))
    assert not ratelimit.hit("register:ip", "203.0.113.7", 5, 3600)

    with connection.cursor() as cursor:
        cursor.execute("SELECT expires FROM rl_test_cache")
        expires = [row[0] for row in cursor.fetchall()]
    assert len(expires) == 1
    expires_at = expires[0] if timezone.is_aware(expires[0]) else expires[0].replace(tzinfo=dt_timezone.utc)
    assert expires_at > timezone.now() + timedelta(minutes=55)


def test_second_invalid_report_pauses_the_person(make_request, make_voucher, mails):
    request = make_request()
    first = services.assign_next(make_voucher())
    make_voucher()
    make_voucher()

    services.report_invalid(first)  # first report: back to waiting, gets the next voucher
    request.refresh_from_db()
    assert request.status == S.ASSIGNED
    second = request.assignments.get(status=Assignment.Status.ACTIVE)

    services.report_invalid(second)  # second report: paused, no further voucher
    request.refresh_from_db()
    assert request.status == S.PAUSED
    assert Voucher.objects.filter(status=Voucher.Status.RECEIVED).count() == 1
    assert "zweite Meldung" in mails[-1].body


def test_stale_parent_is_not_completed_twice(make_request, make_voucher, monkeypatch):
    make_request()
    parent = services.assign_next(make_voucher())
    services.mark_redeemed(parent)
    services.mark_paid(parent)
    stale = Assignment.objects.get(pk=parent.pk)  # read before the first follow-up arrives
    services.register_replicated_voucher("CHILDCODE0000001")
    assert Assignment.objects.get(pk=parent.pk).status == Assignment.Status.COMPLETED

    # Simulate a second, concurrent mail that found the same parent before it was completed.
    real_find = services._find_parent_assignment
    monkeypatch.setattr(
        services,
        "_find_parent_assignment",
        lambda event, text, exclude=(): stale if not exclude else real_find(event, text, exclude),
    )
    child = services.register_replicated_voucher("CHILDCODE0000002")
    assert child.parent is None
    assert AuditLog.objects.filter(action="completed", target_id=parent.pk).count() == 1


def test_withdraw_does_not_lock_request_before_voucher(make_request, make_voucher):
    holder = make_request()
    nxt = make_request()
    services.assign_next(make_voucher())
    services.withdraw(holder)
    holder.refresh_from_db()
    nxt.refresh_from_db()
    assert holder.status == S.WITHDRAWN
    assert nxt.status == S.ASSIGNED
    with pytest.raises(services.InvalidTransition):
        services.withdraw(holder)
