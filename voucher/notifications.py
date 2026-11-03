"""Outgoing mails. Messages are rendered immediately and sent after the DB commit."""

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import formats, timezone, translation

logger = logging.getLogger(__name__)


def defer(fn):
    """Run ``fn`` after the surrounding transaction commits (immediately if none)."""
    transaction.on_commit(fn, robust=True)


def fmt_deadline(dt):
    with translation.override("de"):
        return formats.date_format(timezone.localtime(dt), "D H:i")


def fmt_datetime(dt):
    with translation.override("de"):
        return formats.date_format(timezone.localtime(dt), "D, d.m.Y H:i")


def admin_recipients():
    if settings.ADMIN_EMAILS:
        return list(settings.ADMIN_EMAILS)
    User = get_user_model()
    return list(User.objects.filter(is_staff=True, is_active=True).values_list("email", flat=True))


def send_mail(to, subject, template, context):
    """Render ``<template>.txt`` and ``<template>.html`` and queue the message."""
    recipients = [to] if isinstance(to, str) else list(to)
    if not recipients:
        logger.warning("No recipients for mail template %s", template)
        return
    context = {"site_url": settings.SITE_URL, "inbox_address": settings.VOUCHER_INBOX_ADDRESS, **context}
    with translation.override("de"):
        text = render_to_string(f"voucher/mail/{template}.txt", context)
        html = render_to_string(f"voucher/mail/{template}.html", context)
    from .models import OutgoingMail

    queued = OutgoingMail.objects.create(recipients=recipients, subject=subject, body_text=text, body_html=html)
    defer(lambda: deliver(queued.pk))


def deliver(pk):
    """Try to send one queued mail. Deletes it on success; records the error otherwise."""
    from .logging import mask_codes
    from .models import OutgoingMail

    with transaction.atomic():
        # skip_locked: the web process and the worker never send the same row twice.
        mail = OutgoingMail.objects.select_for_update(skip_locked=True).filter(pk=pk).first()
        if mail is None:
            return False
        message = EmailMultiAlternatives(mail.subject, mail.body_text, settings.DEFAULT_FROM_EMAIL, mail.recipients)
        if mail.body_html:
            message.attach_alternative(mail.body_html, "text/html")
        try:
            message.send(fail_silently=False)
        except Exception as exc:  # SMTP errors, timeouts, DNS ...
            mail.attempts += 1
            mail.last_attempt_at = timezone.now()
            mail.last_error = mask_codes(f"{type(exc).__name__}: {exc}")[:500]
            mail.save(update_fields=["attempts", "last_attempt_at", "last_error"])
            logger.warning("Sending mail %s failed (attempt %s): %s", mail.pk, mail.attempts, mail.last_error)
            return False
        mail.delete()
    return True


def retry_queued(min_age_seconds=60):
    """Retry unsent mails (called by the worker). Fresh rows are left to the web process."""
    from datetime import timedelta

    from .models import OutgoingMail

    cutoff = timezone.now() - timedelta(seconds=min_age_seconds)
    pks = OutgoingMail.objects.filter(
        created_at__lte=cutoff, attempts__lt=OutgoingMail.MAX_ATTEMPTS
    ).values_list("pk", flat=True)
    results = [deliver(pk) for pk in list(pks)]
    return {"sent": results.count(True), "failed": results.count(False)}


def _subject(event, text):
    return f"[{event.name}] {text}"


def _user_context(assignment):
    from . import tokens

    request = assignment.ticket_request
    return {
        "assignment": assignment,
        "voucher": assignment.voucher,
        "event": request.event,
        "user": request.user,
        "deadline": fmt_deadline(assignment.deadline_at),
        "deadline_long": fmt_datetime(assignment.deadline_at),
        "shop_url": settings.VOUCHER_SHOP_URL,
        "links": tokens.action_links(assignment),
    }


# --- Mails to users -------------------------------------------------------


def assignment_created(assignment):
    event = assignment.ticket_request.event
    ctx = _user_context(assignment)
    send_mail(
        assignment.ticket_request.user.email,
        _subject(event, f"Dein Voucher – bitte bis {ctx['deadline']} einlösen"),
        "assignment",
        ctx,
    )


def redeem_reminder(assignment):
    event = assignment.ticket_request.event
    ctx = _user_context(assignment)
    send_mail(
        assignment.ticket_request.user.email,
        _subject(event, f"Erinnerung: Voucher bis {ctx['deadline']} einlösen"),
        "assignment",
        {**ctx, "reminder": True},
    )


def please_pay(assignment, reminder=False):
    event = assignment.ticket_request.event
    prefix = "Erinnerung: " if reminder else ""
    send_mail(
        assignment.ticket_request.user.email,
        _subject(event, f"{prefix}Eingelöst – bitte bezahlen"),
        "please_pay",
        {**_user_context(assignment), "reminder": reminder},
    )


def message(to, subject, paragraphs, *, greeting=None, link=None, link_label=None):
    """Short mail without voucher code or action buttons."""
    send_mail(
        to,
        subject,
        "message",
        {"greeting": greeting, "paragraphs": paragraphs, "link": link, "link_label": link_label},
    )


def _dashboard_url():
    return settings.SITE_URL + reverse("voucher:dashboard")


def _to_user(assignment, subject, paragraphs):
    request = assignment.ticket_request
    message(
        request.user.email,
        _subject(request.event, subject),
        paragraphs,
        greeting=f"Hallo {request.user.nickname},",
        link=_dashboard_url(),
        link_label="Zum Dashboard",
    )


def paid_confirmation(assignment):
    _to_user(
        assignment,
        "Bezahlt – danke!",
        [
            "deine Bestellung ist als bezahlt markiert. Danke!",
            "Sobald der Folgevoucher bei uns eingeht, geben wir ihn an die nächste Person weiter "
            "und sagen dir Bescheid. Das dauert mindestens 24 Stunden und passiert nur Mo–Fr abends.",
        ],
    )


def chain_completed(assignment):
    _to_user(
        assignment,
        "Folgevoucher eingegangen – abgeschlossen",
        [
            "Dein Folgevoucher ist eingegangen und wurde weitergegeben.",
            "Für dich ist damit alles erledigt. Viel Spaß auf dem Congress!",
        ],
    )


def deadline_missed(assignment, removed):
    if removed:
        tail = "Du stehst deshalb nicht mehr auf der Warteliste. Melde dich bei den Admins, wenn das ein Fehler ist."
    else:
        tail = "Du stehst jetzt wieder am Ende der Warteliste und bekommst automatisch einen neuen Voucher, wenn du dran bist."
    _to_user(
        assignment,
        "Einlösefrist verpasst",
        [
            f"die Einlösefrist für deinen Voucher ist am {fmt_datetime(assignment.deadline_at)} abgelaufen. "
            "Der Voucher wurde an die nächste Person weitergegeben.",
            tail,
        ],
    )


def withdrawn(ticket_request):
    message(
        ticket_request.user.email,
        _subject(ticket_request.event, "Du stehst nicht mehr auf der Warteliste"),
        [
            "du hast angegeben, dass du kein Ticket mehr brauchst. Dein Platz auf der Warteliste ist freigegeben.",
            "Falls du doch wieder ein Ticket möchtest, kannst du dich im Dashboard erneut eintragen (am Listenende).",
        ],
        greeting=f"Hallo {ticket_request.user.nickname},",
        link=_dashboard_url(),
        link_label="Zum Dashboard",
    )


def invalid_confirmation(assignment):
    _to_user(
        assignment,
        "Voucher als ungültig gemeldet – du bleibst vorne",
        [
            "danke für die Meldung. Der Voucher ist als ungültig markiert und die Admins sind informiert.",
            "Du behältst deinen Platz auf der Warteliste. Ein Admin prüft die Meldung und gibt "
            "dich danach wieder frei – dann bekommst du den nächsten freien Voucher.",
        ],
    )


def assignment_revoked(assignment):
    _to_user(
        assignment,
        "Deine Voucher-Zuweisung wurde aufgehoben",
        [
            "ein Admin hat die Zuweisung deines Vouchers aufgehoben. Bitte löse den Code nicht mehr ein.",
            "Dein Platz auf der Warteliste bleibt erhalten, ist aber pausiert. Bei Fragen melde dich bei den Admins.",
        ],
    )


# --- Mails to admins ------------------------------------------------------


def admin_alert(event, subject, paragraphs, *, path=None):
    """Alert all admins. Never include voucher codes; link to the admin dashboard instead."""
    prefix = f"[{event.name}] " if event else ""
    message(
        admin_recipients(),
        f"{prefix}[Admin] {subject}",
        paragraphs,
        link=settings.SITE_URL + (path or reverse("voucher:admin_slots")),
        link_label="Zum Admin-Dashboard",
    )
