import re

import pytest
from django.core.management import call_command
from django.urls import reverse
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice

from accounts import tokens
from accounts.models import User


def link_path(mail):
    match = re.search(r"https://voucher\.example\.de(/\S+/)", mail.body)
    assert match, mail.body
    return match.group(1)


# --- registration --------------------------------------------------------------


def test_registration_flow(client, mails, db):
    response = client.post(reverse("accounts:register"), {"nickname": "nick", "email": "New@Example.org"})
    assert response.status_code == 200
    user = User.objects.get(email="new@example.org")
    assert not user.email_verified and not user.approved
    assert len(mails) == 1

    path = link_path(mails[0])
    client.get(path)  # GET does not verify
    user.refresh_from_db()
    assert not user.email_verified
    client.post(path)
    user.refresh_from_db()
    assert user.email_verified
    assert not user.can_login  # waits for admin approval


def test_registration_does_not_reveal_existing_accounts(client, make_user, mails):
    existing = make_user()
    response = client.post(reverse("accounts:register"), {"nickname": "x", "email": existing.email})
    assert response.status_code == 200
    assert b"Schau in dein Postfach" in response.content
    assert mails == []
    assert User.objects.filter(email=existing.email).count() == 1


def test_registration_rate_limit(client, db):
    codes = [
        client.post(reverse("accounts:register"), {"nickname": "n", "email": f"r{i}@example.org"}).status_code
        for i in range(6)
    ]
    assert codes[:5] == [200] * 5
    assert codes[5] == 429


# --- magic link -------------------------------------------------------------------


def request_link(client, email):
    return client.post(reverse("accounts:login"), {"email": email})


def test_magic_link_login_once(client, make_user, mails):
    user = make_user()
    request_link(client, user.email)
    assert len(mails) == 1
    path = link_path(mails[0])

    response = client.get(path)
    assert b"anmelden?" in response.content
    assert "_auth_user_id" not in client.session  # GET does not log in

    response = client.post(path)
    assert response.status_code == 302
    assert client.session["_auth_user_id"] == str(user.pk)

    client.logout()
    response = client.post(path)  # second use
    assert response.status_code == 400
    assert "_auth_user_id" not in client.session


def test_magic_link_expired(client, make_user, mails, monkeypatch):
    user = make_user()
    request_link(client, user.email)
    path = link_path(mails[0])
    monkeypatch.setattr(tokens, "MAGIC_LINK_MAX_AGE", -1)
    assert client.post(path).status_code == 400
    assert "_auth_user_id" not in client.session


@pytest.mark.parametrize("flags", [{"approved": False}, {"email_verified": False}, {"is_active": False}])
def test_magic_link_not_sent_to_users_who_cannot_log_in(client, make_user, mails, flags):
    user = make_user(**flags)
    response = request_link(client, user.email)
    assert response.status_code == 200
    assert mails == []


def test_magic_link_rejected_after_approval_revoked(client, make_user, mails):
    user = make_user()
    request_link(client, user.email)
    path = link_path(mails[0])
    user.approved = False
    user.save()
    assert client.post(path).status_code == 400
    assert "_auth_user_id" not in client.session


def test_magic_link_rate_limit(client, make_user, mails):
    user = make_user()
    statuses = [request_link(client, user.email).status_code for _ in range(4)]
    assert statuses == [200, 200, 200, 429]
    assert len(mails) == 3


# --- admin login --------------------------------------------------------------------


@pytest.fixture
def admin_user(db):
    user = User.objects.create_superuser(email="admin@example.org", nickname="admin", password="correct horse battery")
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    return user, device


def current_token(device):
    return str(totp(device.bin_key, step=device.step, t0=device.t0, digits=device.digits)).zfill(device.digits)


def admin_post(client, password, token, email="admin@example.org"):
    return client.post(reverse("accounts:admin_login"), {"email": email, "password": password, "otp_token": token})


def test_admin_login_requires_password_and_totp(client, admin_user):
    user, device = admin_user
    assert admin_post(client, "wrong password!!", current_token(device)).status_code == 200
    assert "_auth_user_id" not in client.session
    assert admin_post(client, "correct horse battery", "000000").status_code == 200
    assert "_auth_user_id" not in client.session

    response = admin_post(client, "correct horse battery", current_token(device))
    assert response.status_code == 302
    assert client.session["_auth_user_id"] == str(user.pk)
    assert client.session.get("otp_device_id")


def test_admin_login_rejects_non_staff(client, make_user):
    user = make_user()
    user.set_password("correct horse battery")
    user.save()
    device = TOTPDevice.objects.create(user=user, name="d", confirmed=True)
    admin_post(client, "correct horse battery", current_token(device), email=user.email)
    assert "_auth_user_id" not in client.session


def test_admin_login_rate_limit(client, admin_user):
    user, device = admin_user
    for _ in range(5):
        admin_post(client, "wrong password!!", "000000")
    response = admin_post(client, "correct horse battery", current_token(device))
    assert response.status_code == 429
    assert "_auth_user_id" not in client.session


def test_create_admin_command(db, monkeypatch, capsys):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "a very long passphrase")
    call_command("create_admin", email="Orga@Example.org", nickname="orga")
    user = User.objects.get(email="orga@example.org")
    assert user.is_staff and user.approved and user.email_verified
    assert user.check_password("a very long passphrase")
    assert TOTPDevice.objects.filter(user=user, confirmed=True).count() == 1
    assert "otpauth://" in capsys.readouterr().out


# --- account settings ----------------------------------------------------------------


def test_email_change(client, make_user, mails):
    user = make_user()
    client.force_login(user)
    client.post(reverse("accounts:settings"), {"email-email": "changed@example.org", "change_email": "1"})
    assert mails[-1].to == ["changed@example.org"]
    path = link_path(mails[-1])
    client.get(path)
    user.refresh_from_db()
    assert user.email != "changed@example.org"
    client.post(path)
    user.refresh_from_db()
    assert user.email == "changed@example.org"


def test_delete_account(client, make_user, make_request):
    user = make_user()
    make_request(user=user)
    client.force_login(user)
    client.post(reverse("accounts:settings"), {"delete-confirm": "on", "delete_account": "1"})
    assert not User.objects.filter(pk=user.pk).exists()


def test_delete_account_refused_while_holding_voucher(client, make_user, make_request):
    from voucher.models import TicketRequest

    user = make_user()
    make_request(user=user, status=TicketRequest.Status.REDEEMED)
    client.force_login(user)
    response = client.post(reverse("accounts:settings"), {"delete-confirm": "on", "delete_account": "1"})
    assert b"Du h\xc3\xa4ltst gerade einen Voucher" in response.content
    assert User.objects.filter(pk=user.pk).exists()
