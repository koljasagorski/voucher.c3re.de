"""State transitions. Views, mail links, commands and admins only call into this module.

Rules:
- Every transition runs in ``transaction.atomic()`` and locks the rows it changes.
- A transition that does not match the current state raises :class:`InvalidTransition` and
  changes nothing, so repeated calls (double clicks, old links) are harmless.
- Every transition writes an :class:`AuditLog` entry. Voucher codes never go into it.
"""

import re
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max, Q
from django.utils import timezone

from . import notifications
from .models import Assignment, AuditLog, Event, InboundMail, TicketRequest, Voucher

S = TicketRequest.Status
VS = Voucher.Status
AS = Assignment.Status
Via = AuditLog.Via


class InvalidTransition(Exception):
    """The requested transition is not possible in the current state ("nichts zu tun")."""


# --- helpers --------------------------------------------------------------


def audit(action, target, *, actor=None, via=Via.SYSTEM, **details):
    AuditLog.objects.create(
        actor=actor if (actor is not None and actor.pk) else None,
        via=via,
        action=action,
        target_type=type(target).__name__ if target is not None else "",
        target_id=getattr(target, "pk", None),
        details=details,
    )


def get_active_event():
    return Event.objects.filter(is_active=True).first()


def _lock_event(event):
    return Event.objects.select_for_update().get(pk=event.pk)


def _lock_assignment(assignment):
    """Lock voucher, request and assignment in a fixed order and return fresh rows."""
    voucher = Voucher.objects.select_for_update().get(pk=assignment.voucher_id)
    request = TicketRequest.objects.select_for_update().get(pk=assignment.ticket_request_id)
    assignment = Assignment.objects.select_for_update().get(pk=assignment.pk)
    assignment.voucher = voucher
    assignment.ticket_request = request
    return assignment, voucher, request


def _require(condition, message="nichts zu tun"):
    if not condition:
        raise InvalidTransition(message)


def _end_position(event):
    current = event.requests.aggregate(m=Max("position"))["m"]
    return (current or 0) + 1


def _set_status(obj, status):
    obj.status = status
    obj.save(update_fields=["status"] + (["updated_at"] if hasattr(obj, "updated_at") else []))


# --- waiting list -----------------------------------------------------------


def join_waitlist(event, user, *, via=Via.DASHBOARD):
    """User wants a ticket: append to the end of the list (re-join after withdrawing)."""
    with transaction.atomic():
        event = _lock_event(event)
        _require(event.is_active, "Kein aktives Event")
        existing = TicketRequest.objects.select_for_update().filter(event=event, user=user).first()
        if existing is not None:
            _require(existing.status == S.WITHDRAWN, "Du stehst bereits auf der Liste")
            existing.status = S.WAITING
            existing.position = _end_position(event)
            existing.save(update_fields=["status", "position", "updated_at"])
            request = existing
        else:
            request = TicketRequest.objects.create(
                event=event, user=user, position=_end_position(event), status=S.WAITING
            )
        audit("join", request, actor=user, via=via, position=request.position)
        assign_free_vouchers(event)
    return request


def pause(request, *, actor=None, via=Via.ADMIN):
    with transaction.atomic():
        request = TicketRequest.objects.select_for_update().get(pk=request.pk)
        _require(request.status == S.WAITING)
        _set_status(request, S.PAUSED)
        audit("pause", request, actor=actor, via=via)
    return request


def resume(request, *, actor=None, via=Via.ADMIN):
    with transaction.atomic():
        request = TicketRequest.objects.select_for_update().get(pk=request.pk)
        _require(request.status == S.PAUSED)
        _set_status(request, S.WAITING)
        audit("resume", request, actor=actor, via=via)
        assign_free_vouchers(request.event)
    return request


def skip(request, *, actor=None, via=Via.ADMIN):
    """Move a person one place back, behind the next open (waiting/paused) entry."""
    with transaction.atomic():
        _lock_event(request.event)
        request = TicketRequest.objects.select_for_update().get(pk=request.pk)
        _require(request.status in TicketRequest.OPEN_STATUSES)
        following = (
            TicketRequest.objects.select_for_update()
            .filter(event_id=request.event_id, status__in=TicketRequest.OPEN_STATUSES)
            .filter(Q(position__gt=request.position) | Q(position=request.position, pk__gt=request.pk))
            .order_by("position", "pk")
            .first()
        )
        _require(following is not None, "Niemand dahinter")
        request.position, following.position = following.position, request.position
        if request.position == following.position:  # equal positions: make the order explicit
            request.position += 1
        request.save(update_fields=["position", "updated_at"])
        following.save(update_fields=["position", "updated_at"])
        audit("skip", request, actor=actor, via=via, position=request.position)
    return request


def reorder(event, ordered_ids, *, actor=None, via=Via.ADMIN):
    """Re-sort the given requests in the given order, reusing their existing positions.

    Requests not in ``ordered_ids`` keep their positions, so a filtered list can be sorted
    without disturbing the rest.
    """
    ordered_ids = [int(pk) for pk in ordered_ids]
    with transaction.atomic():
        _lock_event(event)
        requests = {
            r.pk: r
            for r in TicketRequest.objects.select_for_update().filter(event=event, pk__in=ordered_ids)
        }
        _require(len(requests) == len(set(ordered_ids)) == len(ordered_ids), "Ungültige Liste")
        positions = sorted(r.position for r in requests.values())
        changed = []
        for pk, position in zip(ordered_ids, positions):
            request = requests[pk]
            if request.position != position:
                request.position = position
                changed.append(request)
        TicketRequest.objects.bulk_update(changed, ["position"])
        if changed:
            audit("reorder", event, actor=actor, via=via, request_ids=ordered_ids)
    return len(changed)


def move_to(request, rank, *, actor=None, via=Via.ADMIN):
    """Move an open request to ``rank`` (1-based) among the open requests of its event."""
    open_ids = list(
        TicketRequest.objects.filter(event_id=request.event_id, status__in=TicketRequest.OPEN_STATUSES)
        .order_by("position", "pk")
        .values_list("pk", flat=True)
    )
    _require(request.pk in open_ids)
    open_ids.remove(request.pk)
    rank = max(1, min(int(rank), len(open_ids) + 1))
    open_ids.insert(rank - 1, request.pk)
    return reorder(request.event, open_ids, actor=actor, via=via)


def set_note(request, note, *, actor=None):
    request.admin_note = note
    request.save(update_fields=["admin_note", "updated_at"])
    audit("note", request, actor=actor, via=Via.ADMIN)


# --- assigning ----------------------------------------------------------------


def _create_assignment(voucher, request, *, actor, via, allowed_request_statuses):
    """Assign a locked, free voucher to a request. Returns ``None`` if the request is gone."""
    now = timezone.now()
    # Compare-and-set on the request: safe even if another transaction took it meanwhile.
    taken = TicketRequest.objects.filter(pk=request.pk, status__in=allowed_request_statuses).update(
        status=S.ASSIGNED, updated_at=now
    )
    if not taken:
        return None
    request.status = S.ASSIGNED
    try:
        with transaction.atomic():
            assignment = Assignment.objects.create(
                voucher=voucher,
                ticket_request=request,
                assigned_at=now,
                deadline_at=now + timedelta(hours=voucher.event.redeem_deadline_hours),
            )
    except IntegrityError:
        TicketRequest.objects.filter(pk=request.pk).update(status=allowed_request_statuses[0])
        return None
    _set_status(voucher, VS.ASSIGNED)
    audit(
        "assign",
        assignment,
        actor=actor,
        via=via,
        voucher_id=voucher.pk,
        request_id=request.pk,
        deadline=assignment.deadline_at.isoformat(),
    )
    notifications.assignment_created(assignment)
    return assignment


def assign_next(voucher, *, actor=None, via=Via.SYSTEM, exclude_request_ids=()):
    """Give a free voucher to the first waiting person. No-op if auto_assign is off."""
    with transaction.atomic():
        voucher = Voucher.objects.select_for_update().select_related("event").get(pk=voucher.pk)
        if voucher.status != VS.RECEIVED or not voucher.event.auto_assign:
            return None
        candidate = (
            TicketRequest.objects.select_for_update()
            .filter(event_id=voucher.event_id, status=S.WAITING)
            .exclude(pk__in=exclude_request_ids)
            .order_by("position", "created_at", "pk")
            .first()
        )
        if candidate is None:
            return None
        return _create_assignment(
            voucher, candidate, actor=actor, via=via, allowed_request_statuses=(S.WAITING,)
        )


def assign_free_vouchers(event, *, actor=None, via=Via.SYSTEM):
    """Hand out all free vouchers of an event to waiting people (if auto_assign is on)."""
    created = []
    if not Event.objects.filter(pk=event.pk, auto_assign=True).exists():
        return created
    for voucher in Voucher.objects.filter(event=event, status=VS.RECEIVED).order_by("received_at", "pk"):
        assignment = assign_next(voucher, actor=actor, via=via)
        if assignment is None:
            break
        created.append(assignment)
    return created


def assign_to(voucher, request, *, actor=None, via=Via.ADMIN):
    """Admin assigns a free voucher to a specific waiting or paused person."""
    with transaction.atomic():
        voucher = Voucher.objects.select_for_update().select_related("event").get(pk=voucher.pk)
        request = TicketRequest.objects.select_for_update().get(pk=request.pk)
        _require(voucher.status == VS.RECEIVED, "Voucher ist nicht frei")
        _require(request.event_id == voucher.event_id, "Falsches Event")
        _require(request.status in TicketRequest.OPEN_STATUSES, "Person wartet nicht")
        assignment = _create_assignment(
            voucher, request, actor=actor, via=via, allowed_request_statuses=(request.status,)
        )
        _require(assignment is not None)
    return assignment


def create_voucher(event, code, *, actor=None, via=Via.ADMIN):
    """Admin enters a start voucher (source initial)."""
    code = code.strip()
    _require(code, "Kein Code angegeben")
    with transaction.atomic():
        _require(not Voucher.objects.filter(code=code).exists(), "Code ist schon bekannt")
        voucher = Voucher.objects.create(
            event=event, code=code, source=Voucher.Source.INITIAL, received_at=timezone.now()
        )
        audit("voucher_create", voucher, actor=actor, via=via, source=voucher.source)
        assign_next(voucher, actor=actor, via=via)
    return voucher


def set_auto_assign(event, enabled, *, actor=None, via=Via.ADMIN):
    with transaction.atomic():
        event = _lock_event(event)
        if event.auto_assign == enabled:
            return event
        event.auto_assign = enabled
        event.save(update_fields=["auto_assign"])
        audit("auto_assign", event, actor=actor, via=via, enabled=enabled)
        if enabled:
            assign_free_vouchers(event, actor=actor, via=via)
    return event


# --- holder actions -------------------------------------------------------------


def mark_redeemed(assignment, order_number=None, *, actor=None, via=Via.DASHBOARD):
    with transaction.atomic():
        assignment, voucher, request = _lock_assignment(assignment)
        _require(assignment.status == AS.ACTIVE and request.status == S.ASSIGNED)
        assignment.redeemed_at = timezone.now()
        if order_number:
            assignment.order_number = order_number.strip()[:64]
        assignment.save(update_fields=["redeemed_at", "order_number"])
        _set_status(voucher, VS.REDEEMED)
        _set_status(request, S.REDEEMED)
        audit("redeemed", assignment, actor=actor, via=via, has_order_number=bool(order_number))
        notifications.please_pay(assignment)
    return assignment


def mark_paid(assignment, order_number=None, *, actor=None, via=Via.DASHBOARD):
    with transaction.atomic():
        assignment, voucher, request = _lock_assignment(assignment)
        _require(assignment.status == AS.ACTIVE and request.status == S.REDEEMED)
        assignment.paid_at = timezone.now()
        if order_number:
            assignment.order_number = order_number.strip()[:64]
        assignment.save(update_fields=["paid_at", "order_number"])
        _set_status(voucher, VS.PAID)
        _set_status(request, S.PAID)
        audit("paid", assignment, actor=actor, via=via, has_order_number=bool(order_number))
        notifications.paid_confirmation(assignment)
    return assignment


def report_invalid(assignment, *, actor=None, via=Via.DASHBOARD):
    """Holder says the code does not work (e.g. already used by the buyer)."""
    with transaction.atomic():
        assignment, voucher, request = _lock_assignment(assignment)
        _require(assignment.status == AS.ACTIVE and request.status == S.ASSIGNED)
        assignment.status = AS.REVOKED
        assignment.revoke_reason = "invalid"
        assignment.save(update_fields=["status", "revoke_reason"])
        _set_status(voucher, VS.INVALID)
        _set_status(request, S.WAITING)  # position stays unchanged
        audit("report_invalid", assignment, actor=actor, via=via, voucher_id=voucher.pk)
        notifications.invalid_confirmation(assignment)
        notifications.admin_alert(
            request.event,
            f"Voucher #{voucher.pk} funktioniert nicht",
            [
                f"{request.user.nickname} meldet, dass Voucher #{voucher.pk} nicht funktioniert "
                "(z. B. weil der Käufer des Vorgängers ihn selbst eingelöst hat).",
                "Der Voucher ist als ungültig markiert, die Person wartet wieder auf ihrer alten Position.",
            ],
        )
        assign_free_vouchers(request.event)
    return assignment


def withdraw(request, *, actor=None, via=Via.DASHBOARD):
    """User does not need a ticket anymore."""
    with transaction.atomic():
        request = TicketRequest.objects.select_for_update().get(pk=request.pk)
        _require(request.status in (S.WAITING, S.PAUSED, S.ASSIGNED))
        freed = None
        active = request.assignments.filter(status=AS.ACTIVE).first()
        if active is not None:
            active, voucher, request = _lock_assignment(active)
            _require(request.status == S.ASSIGNED and voucher.status == VS.ASSIGNED)
            active.status = AS.REVOKED
            active.revoke_reason = "withdrawn"
            active.save(update_fields=["status", "revoke_reason"])
            _set_status(voucher, VS.RECEIVED)
            freed = voucher
        _set_status(request, S.WITHDRAWN)
        audit("withdraw", request, actor=actor, via=via, freed_voucher_id=getattr(freed, "pk", None))
        notifications.withdrawn(request)
        if freed is not None:
            assign_next(freed)
    return request


# --- admin corrections ------------------------------------------------------------


def revoke(assignment, reason="", *, actor=None, via=Via.ADMIN):
    """Admin cancels an assignment before redemption. The person is paused, not removed."""
    with transaction.atomic():
        assignment, voucher, request = _lock_assignment(assignment)
        _require(assignment.status == AS.ACTIVE and request.status == S.ASSIGNED)
        assignment.status = AS.REVOKED
        assignment.revoke_reason = (reason or "admin")[:200]
        assignment.save(update_fields=["status", "revoke_reason"])
        _set_status(voucher, VS.RECEIVED)
        _set_status(request, S.PAUSED)
        audit("revoke", assignment, actor=actor, via=via, reason=assignment.revoke_reason)
        notifications.assignment_revoked(assignment)
        assign_next(voucher, actor=actor, via=via)
    return assignment


def extend_deadline(assignment, hours, *, actor=None, via=Via.ADMIN):
    hours = int(hours)
    _require(hours > 0, "Ungültige Dauer")
    with transaction.atomic():
        assignment, voucher, request = _lock_assignment(assignment)
        _require(assignment.status == AS.ACTIVE and request.status == S.ASSIGNED)
        base = max(assignment.deadline_at, timezone.now())
        assignment.deadline_at = base + timedelta(hours=hours)
        assignment.redeem_reminder_sent_at = None
        assignment.save(update_fields=["deadline_at", "redeem_reminder_sent_at"])
        audit("extend_deadline", assignment, actor=actor, via=via, deadline=assignment.deadline_at.isoformat())
    return assignment


def mark_voucher_invalid(voucher, *, actor=None, via=Via.ADMIN):
    active = voucher.assignments.filter(status=AS.ACTIVE).first()
    if active is not None:
        return report_invalid(active, actor=actor, via=via)
    with transaction.atomic():
        voucher = Voucher.objects.select_for_update().get(pk=voucher.pk)
        _require(voucher.status == VS.RECEIVED)
        _set_status(voucher, VS.INVALID)
        audit("voucher_invalid", voucher, actor=actor, via=via)
    return voucher


def _complete(assignment, voucher, request, *, actor, via, notify=True):
    assignment.status = AS.COMPLETED
    assignment.save(update_fields=["status"])
    _set_status(voucher, VS.REPLICATED)
    _set_status(request, S.COMPLETED)
    audit("completed", assignment, actor=actor, via=via, voucher_id=voucher.pk)
    if notify:
        notifications.chain_completed(assignment)


def mark_voucher_replicated(voucher, *, actor=None, via=Via.ADMIN):
    """Admin closes a chain link without a known follow-up voucher."""
    with transaction.atomic():
        active = voucher.assignments.filter(status=AS.ACTIVE).first()
        _require(active is not None)
        active, voucher, request = _lock_assignment(active)
        _require(request.status in (S.REDEEMED, S.PAID))
        _complete(active, voucher, request, actor=actor, via=via, notify=False)
    return voucher


def _find_parent_assignment(event, search_text):
    candidates = Assignment.objects.select_related("voucher", "ticket_request").filter(
        ticket_request__event=event,
        status=AS.ACTIVE,
        ticket_request__status__in=(S.REDEEMED, S.PAID),
    )
    if search_text:
        for assignment in candidates.exclude(order_number=""):
            pattern = r"(?<![A-Za-z0-9])" + re.escape(assignment.order_number) + r"(?![A-Za-z0-9])"
            if re.search(pattern, search_text, re.IGNORECASE):
                return assignment
    return candidates.filter(ticket_request__status=S.PAID).order_by("paid_at", "pk").first()


def register_replicated_voucher(code, inbound_mail=None, *, event=None, search_text="", actor=None, via=Via.SYSTEM):
    """A follow-up voucher arrived. Link it to its parent, close the parent link, hand it on."""
    code = code.strip()
    _require(code, "Kein Code angegeben")
    event = event or get_active_event()
    _require(event is not None, "Kein aktives Event")
    with transaction.atomic():
        _require(not Voucher.objects.filter(code=code).exists(), "Code ist schon bekannt")
        parent_assignment = _find_parent_assignment(event, search_text)
        voucher = Voucher.objects.create(
            event=event,
            code=code,
            source=Voucher.Source.REPLICATED,
            parent=parent_assignment.voucher if parent_assignment else None,
            inbound_mail=inbound_mail,
            received_at=timezone.now(),
        )
        audit(
            "voucher_replicated",
            voucher,
            actor=actor,
            via=via,
            parent_id=voucher.parent_id,
            inbound_mail_id=getattr(inbound_mail, "pk", None),
        )
        if parent_assignment is not None:
            locked, parent_voucher, parent_request = _lock_assignment(parent_assignment)
            _complete(locked, parent_voucher, parent_request, actor=actor, via=via)
        if inbound_mail is not None:
            inbound_mail.voucher = voucher
            inbound_mail.parse_status = InboundMail.ParseStatus.PARSED
            inbound_mail.save(update_fields=["voucher", "parse_status"])
        assign_next(voucher, actor=actor, via=via)
    return voucher


def reparent(voucher, new_parent, *, actor=None, via=Via.ADMIN):
    """Admin corrects which voucher a follow-up voucher came from.

    The old parent link is reopened (back to paid) unless it has other children; the new
    parent link is completed if it is still waiting for its follow-up voucher.
    """
    with transaction.atomic():
        voucher = Voucher.objects.select_for_update().get(pk=voucher.pk)
        _require(voucher.source == Voucher.Source.REPLICATED, "Nur Folgevoucher haben Eltern")
        new_parent_id = new_parent.pk if new_parent else None
        _require(voucher.parent_id != new_parent_id)
        _require(new_parent_id != voucher.pk, "Ungültiger Elternvoucher")
        if new_parent is not None:
            _require(new_parent.event_id == voucher.event_id, "Falsches Event")
        old_parent_id = voucher.parent_id
        voucher.parent_id = new_parent_id
        voucher.save(update_fields=["parent"])
        audit("reparent", voucher, actor=actor, via=via, old_parent_id=old_parent_id, new_parent_id=new_parent_id)

        if old_parent_id and not Voucher.objects.filter(parent_id=old_parent_id).exists():
            old = (
                Assignment.objects.filter(voucher_id=old_parent_id, status=AS.COMPLETED)
                .order_by("-assigned_at")
                .first()
            )
            if old is not None and not Assignment.objects.filter(voucher_id=old_parent_id, status=AS.ACTIVE).exists():
                old, old_voucher, old_request = _lock_assignment(old)
                old.status = AS.ACTIVE
                old.save(update_fields=["status"])
                restored = S.PAID if old.paid_at else S.REDEEMED
                _set_status(old_voucher, VS.PAID if old.paid_at else VS.REDEEMED)
                _set_status(old_request, restored)
                audit("reopened", old, actor=actor, via=via)

        if new_parent is not None:
            active = Assignment.objects.filter(voucher_id=new_parent.pk, status=AS.ACTIVE).first()
            if active is not None:
                active, parent_voucher, parent_request = _lock_assignment(active)
                if parent_request.status in (S.REDEEMED, S.PAID):
                    _complete(active, parent_voucher, parent_request, actor=actor, via=via)
    return voucher


# --- inbound mail handled by admins -------------------------------------------------


def attach_code_to_mail(inbound_mail, code, *, actor=None):
    """Admin enters the code for a quarantined / no_code mail."""
    with transaction.atomic():
        mail = InboundMail.objects.select_for_update().get(pk=inbound_mail.pk)
        _require(mail.voucher_id is None and mail.parse_status != InboundMail.ParseStatus.PARSED)
        from .mail_in import mail_text  # local import: mail_in depends on services

        voucher = register_replicated_voucher(
            code, mail, search_text=mail_text(mail.raw), actor=actor, via=Via.ADMIN
        )
        audit("mail_code_entered", mail, actor=actor, via=Via.ADMIN, voucher_id=voucher.pk)
    return voucher


def discard_mail(inbound_mail, note="", *, actor=None):
    with transaction.atomic():
        mail = InboundMail.objects.select_for_update().get(pk=inbound_mail.pk)
        _require(mail.parse_status in (InboundMail.ParseStatus.QUARANTINE, InboundMail.ParseStatus.NO_CODE))
        mail.parse_status = InboundMail.ParseStatus.DISCARDED
        if note:
            mail.admin_note = note
        mail.save(update_fields=["parse_status", "admin_note"])
        audit("mail_discarded", mail, actor=actor, via=Via.ADMIN)
    return mail


# --- periodic -------------------------------------------------------------------


def _expire(assignment, now):
    with transaction.atomic():
        _lock_event(assignment.ticket_request.event)
        assignment, voucher, request = _lock_assignment(assignment)
        if not (assignment.status == AS.ACTIVE and request.status == S.ASSIGNED and assignment.deadline_at <= now):
            return False
        event = request.event
        assignment.status = AS.EXPIRED
        assignment.save(update_fields=["status"])
        _set_status(voucher, VS.RECEIVED)
        removed = event.missed_deadline_policy == Event.MissedDeadlinePolicy.REMOVE
        if removed:
            _set_status(request, S.REMOVED)
        else:
            request.status = S.WAITING
            request.position = _end_position(event)
            request.save(update_fields=["status", "position", "updated_at"])
        audit("expired", assignment, policy=event.missed_deadline_policy, request_id=request.pk)
        notifications.deadline_missed(assignment, removed=removed)
        notifications.admin_alert(
            event,
            f"Frist abgelaufen: {request.user.nickname}",
            [
                f"{request.user.nickname} hat Voucher #{voucher.pk} nicht rechtzeitig eingelöst.",
                "Die Person wurde von der Liste entfernt."
                if removed
                else "Die Person steht jetzt am Listenende.",
                "Der Voucher geht an die nächste wartende Person (falls automatische Zuweisung an ist).",
            ],
        )
        assign_next(voucher, exclude_request_ids=[request.pk])
    return True


def _remind_redeem(assignment, now):
    with transaction.atomic():
        assignment, voucher, request = _lock_assignment(assignment)
        if not (
            assignment.status == AS.ACTIVE
            and request.status == S.ASSIGNED
            and assignment.redeem_reminder_sent_at is None
            and assignment.deadline_at > now
        ):
            return False
        assignment.redeem_reminder_sent_at = now
        assignment.save(update_fields=["redeem_reminder_sent_at"])
        audit("redeem_reminder", assignment)
        notifications.redeem_reminder(assignment)
    return True


def _remind_pay(assignment, now):
    with transaction.atomic():
        assignment, voucher, request = _lock_assignment(assignment)
        due = assignment.redeemed_at and assignment.redeemed_at + timedelta(
            hours=request.event.pay_reminder_hours
        ) <= now
        last = assignment.pay_reminder_sent_at
        if not (
            assignment.status == AS.ACTIVE
            and request.status == S.REDEEMED
            and due
            and (last is None or last <= now - timedelta(hours=24))
        ):
            return False
        assignment.pay_reminder_sent_at = now
        assignment.save(update_fields=["pay_reminder_sent_at"])
        audit("pay_reminder", assignment)
        notifications.please_pay(assignment, reminder=True)
    return True


def process_deadlines(now=None):
    """Send reminders and expire overdue assignments. Safe to run any number of times."""
    now = now or timezone.now()
    active = Assignment.objects.select_related("ticket_request__event").filter(status=AS.ACTIVE)
    result = {"redeem_reminders": 0, "expired": 0, "pay_reminders": 0}

    reminder_window = timedelta(hours=settings.VOUCHER_REDEEM_REMINDER_HOURS)
    for assignment in active.filter(
        ticket_request__status=S.ASSIGNED,
        redeem_reminder_sent_at__isnull=True,
        deadline_at__gt=now,
        deadline_at__lte=now + reminder_window,
    ):
        result["redeem_reminders"] += _remind_redeem(assignment, now)

    for assignment in active.filter(ticket_request__status=S.ASSIGNED, deadline_at__lte=now).order_by("deadline_at"):
        result["expired"] += _expire(assignment, now)

    for assignment in active.filter(ticket_request__status=S.REDEEMED, redeemed_at__isnull=False):
        result["pay_reminders"] += _remind_pay(assignment, now)
    return result
