import json
from pathlib import Path

import pytest
from django.core.management import call_command

from voucher import services
from voucher.mail_in import process_raw_mail
from voucher.models import Assignment, InboundMail, TicketRequest, Voucher

FIXTURES = Path(__file__).parent / "fixtures" / "mails"
PS = InboundMail.ParseStatus
GOOD_AUTH = "mx.example.de; dkim=pass header.d=tickets.events.ccc.de; spf=pass smtp.mailfrom=noreply@tickets.events.ccc.de"


def build_mail(
    body="Your new voucher: https://tickets.events.ccc.de/41c3/redeem?voucher=NEWCODE234567890",
    sender="noreply@tickets.events.ccc.de",
    auth=GOOD_AUTH,
    message_id="<m1@example>",
):
    headers = []
    if auth is not None:
        headers.append(f"Authentication-Results: {auth}")
    headers += [
        f"From: Shop <{sender}>",
        "To: voucher@example.de",
        "Subject: Voucher",
        f"Message-ID: {message_id}",
        "Content-Type: text/plain; charset=utf-8",
    ]
    return "\r\n".join(headers) + "\r\n\r\n" + body + "\r\n"


def admin_mails(outbox):
    return [m for m in outbox if m.to == ["orga@example.de"]]


@pytest.mark.parametrize("fixture", sorted(FIXTURES.glob("*.eml")), ids=lambda p: p.stem)
def test_fixture_mails(fixture, event, make_request, make_voucher, mails):
    """Every ``*.eml`` in tests/fixtures/mails must parse as described in its ``*.json``."""
    expected = json.loads(fixture.with_suffix(".json").read_text())
    # A paid holder with a matching order number, so parent detection can be checked.
    holder = make_request()
    assignment = services.assign_next(make_voucher())
    services.mark_redeemed(assignment, order_number=expected.get("order_number") or "ZZZZZ")
    services.mark_paid(assignment)

    mail = process_raw_mail(fixture.read_bytes())

    assert mail.parse_status == expected["parse_status"]
    if expected["code"]:
        assert mail.voucher.code == expected["code"]
        assert mail.voucher.parent_id == assignment.voucher_id
    else:
        assert mail.voucher is None
    holder.refresh_from_db()


def test_parsed_mail_creates_voucher_and_assigns(event, make_request, mails):
    waiting = make_request()
    mail = process_raw_mail(build_mail())
    assert mail.parse_status == PS.PARSED
    assert mail.voucher.code == "NEWCODE234567890"
    assert mail.voucher.source == Voucher.Source.REPLICATED
    waiting.refresh_from_db()
    assert waiting.status == TicketRequest.Status.ASSIGNED
    assert not admin_mails(mails)


def test_regex_fallback(event):
    mail = process_raw_mail(build_mail(body="Dein neuer Code lautet: QWERTZUI98765432 – viel Spass"))
    assert mail.parse_status == PS.PARSED
    assert mail.voucher.code == "QWERTZUI98765432"


def test_wrong_sender_is_rejected(event, mails):
    mail = process_raw_mail(build_mail(sender="phish@evil.example"))
    assert mail.parse_status == PS.REJECTED_SENDER
    assert not Voucher.objects.exists()
    assert not admin_mails(mails)


def test_allowed_single_address(event):
    mail = process_raw_mail(build_mail(sender="noreply@ccc.de", auth="mx.example.de; dkim=pass header.d=ccc.de"))
    assert mail.parse_status == PS.PARSED


@pytest.mark.parametrize(
    "auth",
    [
        None,  # missing Authentication-Results
        "mx.example.de; dkim=fail header.d=tickets.events.ccc.de; spf=softfail smtp.mailfrom=x@tickets.events.ccc.de",
        "other.example.net; dkim=pass header.d=tickets.events.ccc.de",  # foreign authserv-id
        "mx.example.de; dkim=pass header.d=attacker.example",  # signature not aligned to From
    ],
    ids=["missing", "fail", "foreign-authserv", "unaligned"],
)
def test_failed_authentication_goes_to_quarantine(event, mails, auth):
    mail = process_raw_mail(build_mail(auth=auth))
    assert mail.parse_status == PS.QUARANTINE
    assert not Voucher.objects.exists()
    alerts = admin_mails(mails)
    assert len(alerts) == 1
    assert "NEWCODE234567890" not in alerts[0].body
    assert f"/orga/mails/{mail.pk}/" in alerts[0].body


def test_no_code_alerts_admins(event, mails):
    mail = process_raw_mail(build_mail(body="Danke fuer deine Bestellung."))
    assert mail.parse_status == PS.NO_CODE
    assert len(admin_mails(mails)) == 1


def test_known_code_is_duplicate(event, make_voucher):
    make_voucher(code="NEWCODE234567890")
    mail = process_raw_mail(build_mail())
    assert mail.parse_status == PS.DUPLICATE
    assert mail.voucher is None
    assert Voucher.objects.count() == 1


def test_same_message_id_is_stored_once(event):
    assert process_raw_mail(build_mail()) is not None
    assert process_raw_mail(build_mail()) is None
    assert InboundMail.objects.count() == 1


def test_multiple_codes_go_to_quarantine(event):
    body = (
        "https://tickets.events.ccc.de/x/?voucher=AAAAAAAAAAAAAAA1 "
        "https://tickets.events.ccc.de/x/?voucher=BBBBBBBBBBBBBBB2"
    )
    mail = process_raw_mail(build_mail(body=body))
    assert mail.parse_status == PS.QUARANTINE


def test_no_active_event_goes_to_quarantine(db):
    mail = process_raw_mail(build_mail())
    assert mail.parse_status == PS.QUARANTINE


def test_admin_enters_code_for_quarantined_mail(event, make_request, make_voucher):
    make_request()
    assignment = services.assign_next(make_voucher())
    services.mark_redeemed(assignment)
    services.mark_paid(assignment)
    mail = process_raw_mail(build_mail(auth=None))
    assert mail.parse_status == PS.QUARANTINE

    voucher = services.attach_code_to_mail(mail, "MANUALCODE000001")
    mail.refresh_from_db()
    assert mail.parse_status == PS.PARSED
    assert mail.voucher == voucher
    assert voucher.parent == assignment.voucher
    assert Assignment.objects.get(pk=assignment.pk).status == Assignment.Status.COMPLETED
    with pytest.raises(services.InvalidTransition):
        services.attach_code_to_mail(mail, "OTHER")


def test_admin_discards_mail(event):
    mail = process_raw_mail(build_mail(body="nichts"))
    services.discard_mail(mail, "Spam")
    mail.refresh_from_db()
    assert mail.parse_status == PS.DISCARDED


class FakeMessage:
    def __init__(self, uid, raw):
        self.uid = uid
        self.obj = type("Obj", (), {"as_bytes": lambda self_: raw.encode()})()


class FakeMailBox:
    flagged = []

    def __init__(self, host):
        self.messages = [FakeMessage("1", build_mail()), FakeMessage("2", build_mail(message_id="<m2@x>"))]

    def login(self, user, password, initial_folder=None):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def fetch(self, criteria, mark_seen=True, bulk=False):
        assert mark_seen is False
        return self.messages

    def flag(self, uid, flag, value):
        FakeMailBox.flagged.append(uid)


def test_fetch_mail_command_stores_and_marks_seen(event, settings, monkeypatch):
    settings.IMAP_HOST, settings.IMAP_USER = "imap.example.de", "voucher@example.de"
    monkeypatch.setattr("voucher.management.commands.fetch_mail.MailBox", FakeMailBox)
    FakeMailBox.flagged = []
    call_command("fetch_mail")
    assert InboundMail.objects.count() == 2
    assert FakeMailBox.flagged == ["1", "2"]
