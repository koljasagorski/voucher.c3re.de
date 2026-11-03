from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone

from accounts.models import User
from voucher.models import Event, TicketRequest, Voucher


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _send_mails_immediately(monkeypatch):
    """Tests run inside a transaction that never commits, so on_commit would never fire."""
    monkeypatch.setattr("voucher.notifications.defer", lambda fn: fn())


@pytest.fixture
def mails(db):
    # Depends on ``db``: Django's test setup replaces ``mail.outbox`` with a new list.
    from django.core import mail

    mail.outbox.clear()
    return mail.outbox


@pytest.fixture
def event(db):
    now = timezone.now()
    return Event.objects.create(
        name="41C3",
        slug="41c3",
        phase_start=now - timedelta(days=1),
        phase_end=now + timedelta(days=17),
        is_active=True,
    )


_counter = {"n": 0}


@pytest.fixture
def make_user(db):
    def _make(nickname=None, **extra):
        _counter["n"] += 1
        n = _counter["n"]
        extra.setdefault("email_verified", True)
        extra.setdefault("approved", True)
        return User.objects.create_user(
            email=f"user{n}@example.org", nickname=nickname or f"user{n}", **extra
        )

    return _make


@pytest.fixture
def make_request(event, make_user):
    def _make(user=None, status=TicketRequest.Status.WAITING, position=None, ev=None):
        ev = ev or event
        if position is None:
            last = ev.requests.order_by("-position").first()
            position = (last.position if last else 0) + 1
        return TicketRequest.objects.create(
            event=ev, user=user or make_user(), position=position, status=status
        )

    return _make


@pytest.fixture
def make_voucher(event):
    def _make(code=None, status=Voucher.Status.RECEIVED, ev=None, source=Voucher.Source.INITIAL):
        _counter["n"] += 1
        return Voucher.objects.create(
            event=ev or event,
            code=code or f"CODE{_counter['n']:012d}",
            source=source,
            received_at=timezone.now(),
            status=status,
        )

    return _make
