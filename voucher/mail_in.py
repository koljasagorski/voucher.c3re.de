"""Inbound mail: authenticity checks, code extraction, hand-off to the service layer.

The real replication mail format is not known yet. Everything format-specific lives in
``extract_codes`` and is covered by fixture mails in ``tests/fixtures/mails/``.
"""

import email
import hashlib
import html
import logging
import re
from email import policy
from email.utils import parseaddr
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import notifications, services
from .models import InboundMail, Voucher

logger = logging.getLogger(__name__)

PS = InboundMail.ParseStatus
TAG_RE = re.compile(r"<[^>]+>")
COMMENT_RE = re.compile(r"\([^)]*\)")


# --- parsing helpers --------------------------------------------------------------


def parse_message(raw):
    if isinstance(raw, str):
        return email.message_from_string(raw, policy=policy.default)
    return email.message_from_bytes(raw, policy=policy.default)


def _parts(msg):
    """Yield (content_type, decoded text) of all text parts."""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html") or part.get_filename():
            continue
        try:
            text = part.get_content()
        except Exception:  # broken charset etc.
            payload = part.get_payload(decode=True) or b""
            text = payload.decode("utf-8", errors="replace")
        yield ctype, text


def mail_text(raw_or_msg):
    """Human-readable text of a mail (plain parts + HTML parts without tags)."""
    msg = raw_or_msg if isinstance(raw_or_msg, email.message.Message) else parse_message(raw_or_msg)
    chunks = []
    for ctype, text in _parts(msg):
        if ctype == "text/html":
            text = html.unescape(TAG_RE.sub(" ", text))
        chunks.append(text)
    return "\n".join(chunks)


def _url_source(msg):
    """Text including raw HTML so that href targets are searched as well."""
    return "\n".join(html.unescape(text) for _, text in _parts(msg))


def extract_codes(msg):
    """Return the distinct voucher codes found in the mail, in order of appearance.

    1. ``voucher=`` query parameter of links to the ticket shop.
    2. Fallback: ``VOUCHER_CODE_REGEX`` on the readable text (group 1 if the regex has one).
    """
    codes = []
    url_re = re.compile(r"https?://" + re.escape(settings.VOUCHER_SHOP_HOST) + r"/[^\s\"'<>]+", re.I)
    for url in url_re.findall(_url_source(msg)):
        for value in parse_qs(urlsplit(url).query).get("voucher", []):
            value = value.strip()
            if value and value not in codes:
                codes.append(value)
    if codes:
        return codes
    regex = re.compile(settings.VOUCHER_CODE_REGEX)
    for match in regex.finditer(mail_text(msg)):
        value = (match.group(1) if regex.groups else match.group(0)).strip()
        if value and value not in codes:
            codes.append(value)
    return codes


def sender_allowed(from_address):
    address = from_address.lower()
    domain = address.rpartition("@")[2]
    for entry in settings.VOUCHER_ALLOWED_SENDERS:
        if "@" in entry:
            if address == entry:
                return True
        elif domain == entry.lstrip("@"):
            return True
    return False


def _aligned(domain, from_domain):
    domain = domain.lower().strip(".")
    return bool(domain) and (
        domain == from_domain or from_domain.endswith("." + domain) or domain.endswith("." + from_domain)
    )


def check_authentication(msg, from_domain):
    """Evaluate our own server's Authentication-Results header.

    Only the first header carrying our authserv-id counts (our MTA adds it on top).
    Accepted: ``dkim=pass`` with ``header.d``/``header.i`` aligned to the From domain, or
    ``spf=pass`` with ``smtp.mailfrom`` aligned to the From domain.
    Returns ``(ok, header_text)``.
    """
    authserv_id = settings.VOUCHER_AUTHSERV_ID.strip().lower()
    if not authserv_id:
        return False, ""
    for header in msg.get_all("Authentication-Results") or []:
        text = " ".join(str(header).split())
        resinfos = [r.strip() for r in COMMENT_RE.sub("", text).split(";")]
        if not resinfos or not resinfos[0] or resinfos[0].split()[0].lower() != authserv_id:
            continue
        for resinfo in resinfos[1:]:
            tokens = resinfo.split()
            if not tokens or "=" not in tokens[0]:
                continue
            method, _, result = tokens[0].lower().partition("=")
            props = dict(t.split("=", 1) for t in tokens[1:] if "=" in t)
            props = {k.lower(): v for k, v in props.items()}
            if result != "pass":
                continue
            if method == "dkim":
                domain = props.get("header.d") or props.get("header.i", "").rpartition("@")[2]
                if _aligned(domain, from_domain):
                    return True, text
            elif method == "spf":
                domain = props.get("smtp.mailfrom", "").rpartition("@")[2]
                if _aligned(domain, from_domain):
                    return True, text
        return False, text
    return False, ""


# --- processing ----------------------------------------------------------------------


def _alert(mail, subject, paragraphs):
    from django.urls import reverse

    notifications.admin_alert(
        services.get_active_event(),
        subject,
        paragraphs,
        path=reverse("voucher:admin_mail", args=[mail.pk]),
    )


def process_raw_mail(raw):
    """Store and process one mail. Returns the InboundMail, or ``None`` if already known."""
    raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else raw
    msg = parse_message(raw_bytes)
    message_id = (msg.get("Message-ID") or "").strip()
    if not message_id:
        message_id = "<sha256-%s@generated>" % hashlib.sha256(raw_bytes).hexdigest()
    message_id = message_id[:512]
    if InboundMail.objects.filter(message_id=message_id).exists():
        return None

    from_address = parseaddr(str(msg.get("From", "")))[1].lower()
    from_domain = from_address.rpartition("@")[2]
    auth_ok, auth_header = check_authentication(msg, from_domain)
    try:
        with transaction.atomic():
            mail = InboundMail.objects.create(
                received_at=timezone.now(),
                message_id=message_id,
                from_address=from_address[:320],
                subject=str(msg.get("Subject", ""))[:500],
                raw=raw_bytes.decode("utf-8", errors="replace"),
                auth_results=auth_header,
                parse_status=PS.QUARANTINE,
            )
    except IntegrityError:
        return None

    def finish(status, note=""):
        mail.parse_status = status
        mail.admin_note = note
        mail.save(update_fields=["parse_status", "admin_note"])
        services.audit("mail_received", mail, parse_status=status)
        logger.info("Inbound mail %s: %s", mail.pk, status)
        return mail

    if not sender_allowed(from_address):
        return finish(PS.REJECTED_SENDER)

    if not auth_ok:
        finish(PS.QUARANTINE, "Keine bestandene DKIM-/SPF-Prüfung unseres Mailservers.")
        _alert(mail, "Mail in Quarantäne", [
            f"Eine Mail von {from_address} hat die Echtheitsprüfung (DKIM/SPF) nicht bestanden.",
            "Bitte im Mail-Log prüfen und den Code gegebenenfalls manuell eintragen.",
        ])
        return mail

    codes = extract_codes(msg)
    if not codes:
        finish(PS.NO_CODE)
        _alert(mail, "Mail ohne Voucher-Code", [
            f"In einer Mail von {from_address} wurde kein Voucher-Code gefunden.",
            "Bitte im Mail-Log prüfen und den Code gegebenenfalls manuell eintragen.",
        ])
        return mail

    if len(codes) > 1:
        finish(PS.QUARANTINE, f"Mehrere Codes gefunden ({len(codes)}).")
        _alert(mail, "Mail mit mehreren Codes", [
            f"Eine Mail von {from_address} enthält {len(codes)} verschiedene mögliche Codes.",
            "Bitte im Mail-Log prüfen und den richtigen Code manuell eintragen.",
        ])
        return mail

    code = codes[0]
    if Voucher.objects.filter(code=code).exists():
        return finish(PS.DUPLICATE)

    try:
        services.register_replicated_voucher(code, mail, search_text=mail_text(msg))
    except services.InvalidTransition as exc:
        finish(PS.QUARANTINE, str(exc))
        _alert(mail, "Mail konnte nicht verarbeitet werden", [
            f"Eine Mail von {from_address} konnte nicht automatisch verarbeitet werden: {exc}.",
        ])
        return mail
    mail.refresh_from_db()
    services.audit("mail_received", mail, parse_status=mail.parse_status, voucher_id=mail.voucher_id)
    return mail
