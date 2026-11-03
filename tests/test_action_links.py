from datetime import timedelta

import pytest
from django.core import signing
from django.urls import reverse
from django.utils import timezone

from voucher import services, tokens
from voucher.models import Assignment, AuditLog, TicketRequest, Voucher

S = TicketRequest.Status


@pytest.fixture
def assignment(make_request, make_voucher):
    make_request()
    return services.assign_next(make_voucher())


def url(assignment, action):
    return reverse("voucher:action", args=[tokens.make_token(assignment, action)])


def status_of(assignment):
    return TicketRequest.objects.get(pk=assignment.ticket_request_id).status


def test_mail_contains_action_links_without_code_in_url(assignment):
    from django.core.mail import outbox as mails

    body = mails[-1].body
    assert body.count("https://voucher.example.de/a/") == 3
    for link in [line.split()[-1] for line in body.splitlines() if "/a/" in line]:
        assert assignment.voucher.code not in link
        assert tokens.resolve(link.rstrip("/").rsplit("/", 1)[-1])[0] == assignment
    html = mails[-1].alternatives[0][0]
    assert html.count('href="https://voucher.example.de/a/') == 3


def test_get_shows_confirmation_without_state_change(client, assignment):
    response = client.get(url(assignment, "redeemed"))
    assert response.status_code == 200
    assert b"Ja, eingel" in response.content
    assert b'method="post"' in response.content
    assert status_of(assignment) == S.ASSIGNED
    assert not AuditLog.objects.filter(action="redeemed").exists()


def test_valid_post_executes_once_and_replay_does_nothing(client, assignment, mails):
    link = url(assignment, "redeemed")
    response = client.post(link, {"order_number": "XY7Q9"})
    assert b"Erledigt" in response.content
    assert status_of(assignment) == S.REDEEMED
    entry = AuditLog.objects.get(action="redeemed")
    assert entry.via == AuditLog.Via.MAIL_LINK
    assert entry.actor == assignment.ticket_request.user
    assert Assignment.objects.get(pk=assignment.pk).order_number == "XY7Q9"
    mail_count = len(mails)

    # Double click / replay
    response = client.post(link, {"order_number": "OTHER"})
    assert b"Nichts zu tun" in response.content
    assert AuditLog.objects.filter(action="redeemed").count() == 1
    assert Assignment.objects.get(pk=assignment.pk).order_number == "XY7Q9"
    assert len(mails) == mail_count
    # An old link for the previous step is also inert now.
    assert b"Nichts zu tun" in client.get(link).content


def test_full_flow_via_links(client, assignment):
    client.post(url(assignment, "redeemed"))
    client.post(url(assignment, "paid"))
    assert status_of(assignment) == S.PAID
    assert Voucher.objects.get(pk=assignment.voucher_id).status == Voucher.Status.PAID


def test_link_for_wrong_step_does_nothing(client, assignment):
    response = client.post(url(assignment, "paid"))
    assert b"Nichts zu tun" in response.content
    assert status_of(assignment) == S.ASSIGNED


def test_expired_link(client, assignment):
    event = assignment.ticket_request.event
    event.phase_end = timezone.now() - timedelta(minutes=1)
    event.save()
    response = client.post(url(assignment, "redeemed"))
    assert b"Nichts zu tun" in response.content
    assert status_of(assignment) == S.ASSIGNED


def test_foreign_assignment(client, assignment, make_user):
    forged = signing.dumps(
        {"a": assignment.pk, "u": make_user().pk, "x": "withdraw"}, salt=tokens.SALT, compress=True
    )
    response = client.post(reverse("voucher:action", args=[forged]))
    assert b"Nichts zu tun" in response.content
    assert status_of(assignment) == S.ASSIGNED


def test_tampered_token(client, assignment):
    token = tokens.make_token(assignment, "withdraw")
    response = client.post(reverse("voucher:action", args=[token[:-2] + "xx"]))
    assert b"Nichts zu tun" in response.content
    assert status_of(assignment) == S.ASSIGNED


def test_revoked_assignment(client, assignment):
    link = url(assignment, "invalid")
    services.revoke(assignment, "test")
    response = client.post(link)
    assert b"Nichts zu tun" in response.content
    assert Voucher.objects.get(pk=assignment.voucher_id).status != Voucher.Status.INVALID


def test_withdraw_via_link(client, assignment, make_request):
    nxt = make_request()
    client.post(url(assignment, "withdraw"))
    assert status_of(assignment) == S.WITHDRAWN
    nxt.refresh_from_db()
    assert nxt.status == S.ASSIGNED


def test_invalid_via_link_alerts_admins(client, assignment, mails):
    mails.clear()
    client.post(url(assignment, "invalid"))
    assert Voucher.objects.get(pk=assignment.voucher_id).status == Voucher.Status.INVALID
    assert any(m.to == ["orga@example.de"] for m in mails)
