import smtplib
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone
from gunicorn.glogging import Logger

from config.gunicorn_logger import MaskingLogger, mask_atoms
from tests.test_dashboards import admin_client  # noqa: F401  (fixture)
from voucher import services
from voucher.logging import mask_codes
from voucher.models import OutgoingMail, TicketRequest


@pytest.fixture
def smtp_down(monkeypatch):
    def fail(self, messages):
        raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")

    monkeypatch.setattr("django.core.mail.backends.locmem.EmailBackend.send_messages", fail)
    return monkeypatch


def make_old(seconds=120):
    OutgoingMail.objects.update(created_at=timezone.now() - timedelta(seconds=seconds))


def test_sent_mail_is_removed_from_queue(event, make_request, make_voucher, mails):
    make_request()
    services.assign_next(make_voucher())
    assert len(mails) == 1
    assert not OutgoingMail.objects.exists()  # no codes left in the database


def test_failed_mail_is_kept_and_retried(event, make_request, make_voucher, mails, smtp_down):
    request = make_request()
    voucher = make_voucher(code="QUEUEDCODE000001")
    services.assign_next(voucher)

    # The transition is committed even though SMTP is down.
    request.refresh_from_db()
    assert request.status == TicketRequest.Status.ASSIGNED
    queued = OutgoingMail.objects.get()
    assert queued.attempts == 1
    assert "SMTPServerDisconnected" in queued.last_error
    assert queued.recipients == [request.user.email]
    assert mails == []

    # Fresh rows are left alone by the worker (the web process just tried).
    call_command("send_queued_mail")
    assert OutgoingMail.objects.get().attempts == 1

    make_old()
    call_command("send_queued_mail")
    assert OutgoingMail.objects.get().attempts == 2

    smtp_down.undo()  # SMTP is back
    call_command("send_queued_mail")
    assert not OutgoingMail.objects.exists()
    assert len(mails) == 1
    assert "QUEUEDCODE000001" in mails[0].body
    assert mails[0].alternatives  # HTML part survives the queue


def test_retry_gives_up_after_max_attempts(event, make_request, make_voucher, smtp_down):
    make_request()
    services.assign_next(make_voucher())
    make_old()
    OutgoingMail.objects.update(attempts=OutgoingMail.MAX_ATTEMPTS)
    call_command("send_queued_mail")
    assert OutgoingMail.objects.get().attempts == OutgoingMail.MAX_ATTEMPTS


def test_admin_sees_unsent_mail_banner(admin_client, event, make_request, make_voucher, smtp_down):  # noqa: F811
    make_request()
    services.assign_next(make_voucher())
    html = admin_client.get(reverse("voucher:admin_slots")).content.decode()
    assert "1 Mail konnte noch nicht verschickt werden" in html


def test_purge_removes_old_unsent_mails(db):
    mail = OutgoingMail.objects.create(recipients=["a@b"], subject="s", body_text="t")
    OutgoingMail.objects.filter(pk=mail.pk).update(created_at=timezone.now() - timedelta(days=91))
    call_command("purge_old_data")
    assert not OutgoingMail.objects.exists()


# --- tokens in logs ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/a/eyJhIjoxfQ:1xODTc:abc-DEF_123/",
        "/konto/anmelden/eyJ1IjoyfQ:1xODTc:pmo_GWHt76/",
        "/konto/bestaetigen/eyJ1IjoyfQ:1xODTc:pmo_GWHt76/",
        "/konto/email-aendern/eyJ1IjoyfQ:1xODTc:pmo_GWHt76/",
    ],
)
def test_tokens_in_paths_are_masked(path):
    token = path.rstrip("/").rsplit("/", 1)[1]
    masked = mask_codes(f'"POST {path} HTTP/1.1" 200')
    assert token not in masked
    assert "***" in masked


def test_plain_paths_are_not_masked():
    assert mask_codes("GET /konto/anmelden/ HTTP/1.1") == "GET /konto/anmelden/ HTTP/1.1"


def test_gunicorn_access_log_atoms_are_masked():
    atoms = {
        "r": "POST /a/eyJhIjoxfQ:1xODTc:abc/ HTTP/1.1",
        "U": "/a/eyJhIjoxfQ:1xODTc:abc/",
        "f": "https://voucher.example.de/konto/anmelden/eyJ1IjoyfQ:1x:pmo/",
        "s": "200",
        "B": 512,
    }
    masked = mask_atoms(atoms)
    assert all("eyJ" not in str(value) for value in masked.values())
    assert masked["s"] == "200" and masked["B"] == 512
    assert issubclass(MaskingLogger, Logger)
