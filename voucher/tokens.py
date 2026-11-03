"""Signed action tokens for mail links. No DB table; validity is checked against live state."""

from django.conf import settings
from django.core import signing
from django.urls import reverse
from django.utils import timezone

from .models import Assignment

SALT = "voucher.action-link"
ACTIONS = ("redeemed", "paid", "invalid", "withdraw")


class TokenError(Exception):
    """Token is malformed, expired or no longer applicable."""


def make_token(assignment, action):
    if action not in ACTIONS:
        raise ValueError(action)
    payload = {"a": assignment.pk, "u": assignment.ticket_request.user_id, "x": action}
    return signing.dumps(payload, salt=SALT, compress=True)


def action_url(assignment, action):
    return settings.SITE_URL + reverse("voucher:action", args=[make_token(assignment, action)])


def action_links(assignment):
    return {action: action_url(assignment, action) for action in ACTIONS}


def resolve(token):
    """Return ``(assignment, action)`` or raise :class:`TokenError`."""
    try:
        payload = signing.loads(token, salt=SALT)
        assignment_id, user_id, action = payload["a"], payload["u"], payload["x"]
    except (signing.BadSignature, KeyError, TypeError):
        raise TokenError("invalid") from None
    if action not in ACTIONS:
        raise TokenError("invalid")
    assignment = (
        Assignment.objects.select_related("voucher", "ticket_request__event", "ticket_request__user")
        .filter(pk=assignment_id)
        .first()
    )
    if assignment is None or assignment.ticket_request.user_id != user_id:
        raise TokenError("invalid")
    if timezone.now() > assignment.ticket_request.event.phase_end:
        raise TokenError("expired")
    if assignment.status != Assignment.Status.ACTIVE:
        raise TokenError("inactive")
    return assignment, action
