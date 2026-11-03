import pytest
from django.urls import reverse
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice

from accounts.models import User
from voucher import services
from voucher.mail_in import process_raw_mail
from voucher.models import Assignment, AuditLog, InboundMail, TicketRequest, Voucher

S = TicketRequest.Status


# --- user dashboard -------------------------------------------------------------------


def test_dashboard_requires_login(client, db):
    response = client.get(reverse("voucher:dashboard"))
    assert response.status_code == 302
    assert reverse("accounts:login") in response["Location"]


def test_join_and_waiting_position(client, event, make_user, make_request):
    make_request()
    make_request()
    user = make_user()
    client.force_login(user)
    response = client.get(reverse("voucher:dashboard"))
    assert "Ich möchte ein Ticket" in response.content.decode()

    client.post(reverse("voucher:dashboard_action"), {"action": "join"})
    response = client.get(reverse("voucher:dashboard"))
    html = response.content.decode()
    assert "Vor dir warten <strong>2</strong> Personen" in html
    assert "Voucher im Umlauf: <strong>0</strong>" in html


def test_holder_sees_code_and_buttons_others_do_not(client, event, make_request, make_voucher):
    holder = make_request()
    other = make_request()
    voucher = make_voucher(code="SECRETCODE123456")
    services.assign_next(voucher)

    client.force_login(holder.user)
    html = client.get(reverse("voucher:dashboard")).content.decode()
    assert "SECRETCODE123456" in html
    for label in ("Eingelöst", "Voucher funktioniert nicht", "Brauche kein Ticket mehr"):
        assert label in html
    assert 'value="paid"' not in html

    client.force_login(other.user)
    html = client.get(reverse("voucher:dashboard")).content.decode()
    assert "SECRETCODE123456" not in html
    assert 'value="redeemed"' not in html


def test_dashboard_actions(client, event, make_request, make_voucher):
    holder = make_request()
    services.assign_next(make_voucher())
    client.force_login(holder.user)

    client.post(reverse("voucher:dashboard_action"), {"action": "redeemed", "order_number": "AB123"})
    holder.refresh_from_db()
    assert holder.status == S.REDEEMED
    assert AuditLog.objects.get(action="redeemed").via == AuditLog.Via.DASHBOARD
    html = client.get(reverse("voucher:dashboard")).content.decode()
    assert 'value="paid"' in html and 'value="redeemed"' not in html

    # Not applicable in this state -> nothing happens
    client.post(reverse("voucher:dashboard_action"), {"action": "withdraw"})
    holder.refresh_from_db()
    assert holder.status == S.REDEEMED

    client.post(reverse("voucher:dashboard_action"), {"action": "paid"})
    holder.refresh_from_db()
    assert holder.status == S.PAID


def test_withdraw_from_waiting(client, event, make_request):
    request = make_request()
    client.force_login(request.user)
    client.post(reverse("voucher:dashboard_action"), {"action": "withdraw"})
    request.refresh_from_db()
    assert request.status == S.WITHDRAWN


# --- admin dashboard -----------------------------------------------------------------------


@pytest.fixture
def admin_client(client, db):
    user = User.objects.create_superuser(email="admin@example.org", nickname="admin", password="correct horse battery")
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    token = str(totp(device.bin_key, step=device.step, t0=device.t0, digits=device.digits)).zfill(device.digits)
    response = client.post(
        reverse("accounts:admin_login"),
        {"email": user.email, "password": "correct horse battery", "otp_token": token},
    )
    assert response.status_code == 302
    client.admin_user = user
    return client


ADMIN_PAGES = [
    "voucher:admin_waitlist",
    "voucher:admin_slots",
    "voucher:admin_mails",
    "voucher:admin_users",
    "voucher:admin_audit",
    "voucher:admin_events",
    "voucher:admin_event_new",
]


@pytest.mark.parametrize("name", ADMIN_PAGES)
def test_admin_pages_require_otp_verified_staff(client, make_user, name):
    staff = make_user(is_staff=True)
    client.force_login(staff)  # logged in, but not OTP-verified (e.g. via magic link)
    response = client.get(reverse(name))
    assert response.status_code == 302
    assert reverse("accounts:admin_login") in response["Location"]


@pytest.mark.parametrize("name", ADMIN_PAGES)
def test_admin_pages_render(admin_client, event, make_request, make_voucher, name):
    make_request()
    services.assign_next(make_voucher())
    make_voucher()
    assert admin_client.get(reverse(name)).status_code == 200


def test_django_admin_requires_otp(client, make_user):
    staff = make_user(is_staff=True, is_superuser=True)
    client.force_login(staff)
    assert client.get("/django-admin/").status_code == 302


def test_admin_reorder_via_htmx(admin_client, event, make_request):
    a, b, c = make_request(), make_request(), make_request()
    response = admin_client.post(
        reverse("voucher:admin_reorder"), {"ids": f"{c.pk},{a.pk},{b.pk}", "event": event.slug}, HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 200
    assert response["HX-Refresh"] == "true"
    assert list(event.requests.order_by("position").values_list("pk", flat=True)) == [c.pk, a.pk, b.pk]


def test_admin_note_and_csv(admin_client, event, make_request):
    request = make_request()
    response = admin_client.post(reverse("voucher:admin_note", args=[request.pk]), {"admin_note": "kommt spät"})
    assert "gespeichert" in response.content.decode()
    request.refresh_from_db()
    assert request.admin_note == "kommt spät"

    csv = admin_client.get(reverse("voucher:admin_export") + f"?event={event.slug}").content.decode("utf-8-sig")
    lines = csv.strip().splitlines()
    assert lines[0] == "Position;Nickname;E-Mail;Status;Eingetragen;Notiz"
    assert request.user.email in lines[1] and "kommt spät" in lines[1]


def test_admin_actions(admin_client, event, make_request, make_voucher, mails):
    first = make_request()
    second = make_request()
    url = reverse("voucher:admin_action")

    admin_client.post(url, {"op": "create_voucher", "event_id": event.pk, "code": "STARTCODE0000001"})
    voucher = Voucher.objects.get(code="STARTCODE0000001")
    assert voucher.source == Voucher.Source.INITIAL
    first.refresh_from_db()
    assert first.status == S.ASSIGNED

    assignment = Assignment.objects.get(voucher=voucher, status="active")
    admin_client.post(url, {"op": "extend", "assignment": assignment.pk, "hours": "5"})
    admin_client.post(url, {"op": "revoke", "assignment": assignment.pk})
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.status == S.PAUSED
    assert second.status == S.ASSIGNED

    admin_client.post(url, {"op": "resume", "request": first.pk})
    first.refresh_from_db()
    assert first.status == S.WAITING

    admin_client.post(url, {"op": "auto_assign", "event_id": event.pk, "enabled": "0"})
    event.refresh_from_db()
    assert event.auto_assign is False
    assert AuditLog.objects.filter(action="auto_assign", via="admin", actor=admin_client.admin_user).exists()

    # Invalid action shows a message and changes nothing
    response = admin_client.post(url, {"op": "pause", "request": second.pk}, follow=True)
    assert "Nichts zu tun" in response.content.decode()


def test_admin_approves_user(admin_client, make_user, mails):
    pending = make_user(approved=False)
    admin_client.post(reverse("voucher:admin_action"), {"op": "approve", "user": pending.pk})
    pending.refresh_from_db()
    assert pending.approved
    assert mails[-1].to == [pending.email]


def test_admin_enters_code_for_quarantined_mail(admin_client, event, make_request):
    make_request()
    raw = (
        "From: Shop <noreply@tickets.events.ccc.de>\r\nMessage-ID: <q1@x>\r\nSubject: x\r\n\r\n"
        "https://tickets.events.ccc.de/41c3/?voucher=QUARANTINED00001\r\n"
    )
    mail = process_raw_mail(raw)
    assert mail.parse_status == InboundMail.ParseStatus.QUARANTINE
    page = admin_client.get(reverse("voucher:admin_mail", args=[mail.pk])).content.decode()
    assert "Code manuell eintragen" in page

    admin_client.post(reverse("voucher:admin_action"), {"op": "mail_code", "mail": mail.pk, "code": "QUARANTINED00001"})
    mail.refresh_from_db()
    assert mail.parse_status == InboundMail.ParseStatus.PARSED
    assert mail.voucher.code == "QUARANTINED00001"


def test_admin_event_create(admin_client, event):
    response = admin_client.post(
        reverse("voucher:admin_event_new"),
        {
            "name": "42C3",
            "slug": "42c3",
            "phase_start": "2027-11-01T10:00",
            "phase_end": "2027-11-20T10:00",
            "redeem_deadline_hours": 24,
            "pay_reminder_hours": 24,
            "auto_assign": "on",
            "missed_deadline_policy": "end_of_list",
            "is_active": "on",
        },
    )
    assert response.status_code == 302
    event.refresh_from_db()
    assert not event.is_active
    assert services.get_active_event().slug == "42c3"
