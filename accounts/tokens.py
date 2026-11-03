"""Signed, time-limited tokens for account mails. No DB tables."""

from django.conf import settings
from django.core import signing
from django.urls import reverse

from .models import User

VERIFY_SALT = "accounts.verify-email"
CHANGE_SALT = "accounts.change-email"
MAGIC_SALT = "accounts.magic-link"

VERIFY_MAX_AGE = 3 * 24 * 3600
MAGIC_LINK_MAX_AGE = 15 * 60


def _last_login_stamp(user):
    return user.last_login.isoformat() if user.last_login else None


def _url(name, token):
    return settings.SITE_URL + reverse(name, args=[token])


# --- e-mail verification (registration and e-mail change) -------------------------


def email_token(user, email, salt):
    return signing.dumps({"u": user.pk, "e": email.lower()}, salt=salt, compress=True)


def verify_url(user):
    return _url("accounts:verify_email", email_token(user, user.email, VERIFY_SALT))


def change_url(user, new_email):
    return _url("accounts:confirm_email_change", email_token(user, new_email, CHANGE_SALT))


def resolve_email_token(token, salt):
    """Return ``(user, email)`` or ``(None, None)``."""
    try:
        payload = signing.loads(token, salt=salt, max_age=VERIFY_MAX_AGE)
        user = User.objects.filter(pk=payload["u"], is_active=True).first()
        return (user, payload["e"]) if user else (None, None)
    except (signing.BadSignature, KeyError, TypeError):
        return None, None


# --- magic link ------------------------------------------------------------------


def magic_url(user):
    token = signing.dumps({"u": user.pk, "l": _last_login_stamp(user)}, salt=MAGIC_SALT, compress=True)
    return _url("accounts:magic_login", token)


def resolve_magic_token(token):
    """Return the user if the link is fresh, unused and the user may log in, else ``None``.

    A link is bound to the user's ``last_login``; logging in changes it, so every link works
    only once (and older links die with any later login).
    """
    try:
        payload = signing.loads(token, salt=MAGIC_SALT, max_age=MAGIC_LINK_MAX_AGE)
        user = User.objects.filter(pk=payload["u"]).first()
    except (signing.BadSignature, KeyError, TypeError):
        return None
    if user is None or not user.can_login or payload.get("l") != _last_login_stamp(user):
        return None
    return user
