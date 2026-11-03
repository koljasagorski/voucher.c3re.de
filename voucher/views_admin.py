"""Admin dashboard. Every view requires a staff user with an OTP-verified session."""

import csv
from functools import wraps

from django import forms
from django.contrib import messages
from django.db import transaction
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods, require_POST

from accounts import mails as account_mails
from accounts.models import User

from . import services
from .models import Assignment, AuditLog, Event, InboundMail, TicketRequest, Voucher

S = TicketRequest.Status
VS = Voucher.Status
Via = AuditLog.Via


def staff_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        user = request.user
        if not (user.is_authenticated and user.is_staff and user.is_verified()):
            return redirect(f"{reverse('accounts:admin_login')}?next={request.path}")
        return view(request, *args, **kwargs)

    return wrapper


def _selected_event(request):
    slug = request.GET.get("event") or request.POST.get("event")
    if slug:
        return get_object_or_404(Event, slug=slug)
    return services.get_active_event() or Event.objects.first()


def _back(request, default="voucher:admin_waitlist"):
    target = request.POST.get("next") or request.META.get("HTTP_REFERER", "")
    if target and url_has_allowed_host_and_scheme(target, {request.get_host()}, request.is_secure()):
        return redirect(target)
    return redirect(default)


# --- waiting list ------------------------------------------------------------------


@staff_required
def waitlist(request):
    event = _selected_event(request)
    status = request.GET.get("status", "open")
    requests = TicketRequest.objects.none()
    free_vouchers = []
    if event:
        requests = event.requests.select_related("user").order_by("position", "pk")
        if status == "open":
            requests = requests.filter(status__in=TicketRequest.OPEN_STATUSES)
        elif status in S.values:
            requests = requests.filter(status=status)
        free_vouchers = list(event.vouchers.filter(status=VS.RECEIVED))
    counts = dict(event.requests.values_list("status").annotate(n=Count("pk"))) if event else {}
    return render(
        request,
        "voucher/admin/waitlist.html",
        {
            "event": event,
            "events": Event.objects.all(),
            "requests": requests,
            "status": status,
            "statuses": S.choices,
            "counts": counts,
            "free_vouchers": free_vouchers,
            "sortable": status in ("open", S.WAITING, S.PAUSED),
        },
    )


@staff_required
@require_POST
def reorder(request):
    event = _selected_event(request)
    ids = [pk for pk in request.POST.get("ids", "").split(",") if pk.strip().isdigit()]
    try:
        services.reorder(event, ids, actor=request.user)
    except services.InvalidTransition as exc:
        return HttpResponse(f"Fehler: {exc}", status=400)
    response = HttpResponse("Gespeichert")
    response["HX-Refresh"] = "true"
    return response


class NoteForm(forms.Form):
    admin_note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 1}))


@staff_required
@require_POST
def note(request, pk):
    ticket_request = get_object_or_404(TicketRequest, pk=pk)
    form = NoteForm(request.POST)
    if form.is_valid():
        services.set_note(ticket_request, form.cleaned_data["admin_note"], actor=request.user)
    return render(request, "voucher/admin/_note.html", {"r": ticket_request, "saved": True})


@staff_required
def export_csv(request):
    event = _selected_event(request)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="warteliste-{event.slug}.csv"'
    response.write("﻿")  # BOM for spreadsheet programs
    writer = csv.writer(response, delimiter=";")
    writer.writerow(["Position", "Nickname", "E-Mail", "Status", "Eingetragen", "Notiz"])
    for r in event.requests.select_related("user").order_by("position", "pk"):
        writer.writerow(
            [
                r.position,
                r.user.nickname,
                r.user.email,
                r.status,
                timezone.localtime(r.created_at).strftime("%Y-%m-%d %H:%M"),
                r.admin_note,
            ]
        )
    return response


# --- slots / vouchers -----------------------------------------------------------------


@staff_required
def slots(request):
    event = _selected_event(request)
    rows = []
    now = timezone.now()
    open_requests = []
    paid_vouchers = []
    if event:
        vouchers = event.vouchers.select_related("parent").order_by("-received_at")
        if request.GET.get("all") != "1":
            vouchers = vouchers.exclude(status__in=[VS.REPLICATED, VS.INVALID])
        actives = {
            a.voucher_id: a
            for a in Assignment.objects.select_related("ticket_request__user").filter(
                voucher__event=event, status=Assignment.Status.ACTIVE
            )
        }
        for voucher in vouchers:
            assignment = actives.get(voucher.pk)
            alert = False
            if assignment:
                overdue = voucher.status == VS.ASSIGNED and assignment.deadline_at < now
                unpaid = (
                    voucher.status == VS.REDEEMED
                    and assignment.redeemed_at
                    and (now - assignment.redeemed_at).total_seconds() > event.pay_reminder_hours * 3600
                )
                alert = bool(overdue or unpaid)
            rows.append({"voucher": voucher, "assignment": assignment, "alert": alert})
        open_requests = event.requests.filter(status__in=TicketRequest.OPEN_STATUSES).select_related("user")
        paid_vouchers = event.vouchers.filter(status__in=[VS.REDEEMED, VS.PAID, VS.REPLICATED])
    return render(
        request,
        "voucher/admin/slots.html",
        {
            "event": event,
            "events": Event.objects.all(),
            "rows": rows,
            "open_requests": open_requests,
            "parent_candidates": paid_vouchers,
            "show_all": request.GET.get("all") == "1",
        },
    )


# --- one endpoint for all admin actions ---------------------------------------------------


def _int(request, name):
    try:
        return int(request.POST.get(name, ""))
    except ValueError:
        raise services.InvalidTransition("Ungültige Eingabe") from None


@staff_required
@require_POST
def action(request):
    op = request.POST.get("op", "")
    actor = request.user
    try:
        if op in ("pause", "resume", "skip", "move", "assign_to"):
            ticket_request = get_object_or_404(TicketRequest, pk=_int(request, "request"))
            if op == "pause":
                services.pause(ticket_request, actor=actor)
            elif op == "resume":
                services.resume(ticket_request, actor=actor)
            elif op == "skip":
                services.skip(ticket_request, actor=actor)
            elif op == "move":
                services.move_to(ticket_request, _int(request, "rank"), actor=actor)
            else:
                voucher = get_object_or_404(Voucher, pk=_int(request, "voucher"))
                services.assign_to(voucher, ticket_request, actor=actor)
        elif op in ("revoke", "extend"):
            assignment = get_object_or_404(Assignment, pk=_int(request, "assignment"))
            if op == "revoke":
                services.revoke(assignment, request.POST.get("reason", ""), actor=actor)
            else:
                services.extend_deadline(assignment, _int(request, "hours"), actor=actor)
        elif op in ("invalid", "replicated", "reparent"):
            voucher = get_object_or_404(Voucher, pk=_int(request, "voucher"))
            if op == "invalid":
                services.mark_voucher_invalid(voucher, actor=actor, via=Via.ADMIN)
            elif op == "replicated":
                services.mark_voucher_replicated(voucher, actor=actor)
            else:
                parent_id = request.POST.get("parent") or None
                parent = get_object_or_404(Voucher, pk=int(parent_id)) if parent_id else None
                services.reparent(voucher, parent, actor=actor)
        elif op == "create_voucher":
            event = get_object_or_404(Event, pk=_int(request, "event_id"))
            services.create_voucher(event, request.POST.get("code", ""), actor=actor)
        elif op == "auto_assign":
            event = get_object_or_404(Event, pk=_int(request, "event_id"))
            services.set_auto_assign(event, request.POST.get("enabled") == "1", actor=actor)
        elif op in ("mail_code", "mail_discard"):
            mail = get_object_or_404(InboundMail, pk=_int(request, "mail"))
            if op == "mail_code":
                services.attach_code_to_mail(mail, request.POST.get("code", ""), actor=actor)
            else:
                services.discard_mail(mail, request.POST.get("note", ""), actor=actor)
        elif op == "approve":
            user = get_object_or_404(User, pk=_int(request, "user"))
            if user.approved or not user.email_verified:
                raise services.InvalidTransition("nichts zu tun")
            user.approved = True
            user.save(update_fields=["approved"])
            services.audit("approve", user, actor=actor, via=Via.ADMIN)
            account_mails.approved(user)
        else:
            raise services.InvalidTransition("Unbekannte Aktion")
    except services.InvalidTransition as exc:
        messages.error(request, f"Nichts zu tun: {exc}")
    else:
        messages.success(request, "Erledigt.")
    return _back(request)


# --- inbound mails ---------------------------------------------------------------------


@staff_required
def mails(request):
    status = request.GET.get("status", "")
    qs = InboundMail.objects.select_related("voucher").order_by("-received_at")
    if status in InboundMail.ParseStatus.values:
        qs = qs.filter(parse_status=status)
    return render(
        request,
        "voucher/admin/mails.html",
        {"mails": qs[:300], "status": status, "statuses": InboundMail.ParseStatus.choices},
    )


@staff_required
def mail_detail(request, pk):
    from .mail_in import mail_text

    mail = get_object_or_404(InboundMail, pk=pk)
    return render(
        request,
        "voucher/admin/mail_detail.html",
        {
            "mail": mail,
            "text": mail_text(mail.raw),
            "editable": mail.voucher_id is None
            and mail.parse_status in (InboundMail.ParseStatus.QUARANTINE, InboundMail.ParseStatus.NO_CODE),
        },
    )


# --- users, audit log -----------------------------------------------------------------


@staff_required
def users(request):
    return render(
        request,
        "voucher/admin/users.html",
        {
            "pending": User.objects.filter(email_verified=True, approved=False, is_active=True).order_by("date_joined"),
            "unverified": User.objects.filter(email_verified=False).count(),
            "approved": User.objects.filter(approved=True).order_by("nickname"),
        },
    )


@staff_required
def audit_log(request):
    qs = AuditLog.objects.select_related("actor")
    action_name = request.GET.get("action", "")
    if action_name:
        qs = qs.filter(action=action_name)
    return render(request, "voucher/admin/audit.html", {"entries": qs[:500], "action": action_name})


# --- events ------------------------------------------------------------------------------


class EventForm(forms.ModelForm):
    class Meta:
        model = Event
        fields = [
            "name",
            "slug",
            "phase_start",
            "phase_end",
            "redeem_deadline_hours",
            "pay_reminder_hours",
            "auto_assign",
            "missed_deadline_policy",
            "is_active",
        ]
        widgets = {
            "phase_start": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "phase_end": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        }

    def _get_validation_exclusions(self):
        # The view deactivates the previously active event before saving; the DB constraint
        # "only_one_active_event" still guards against races.
        exclusions = super()._get_validation_exclusions()
        exclusions.add("is_active")
        return exclusions

    def clean(self):
        data = super().clean()
        if data.get("phase_start") and data.get("phase_end") and data["phase_end"] <= data["phase_start"]:
            self.add_error("phase_end", "Das Ende muss nach dem Beginn liegen.")
        return data


@staff_required
def events(request):
    return render(request, "voucher/admin/events.html", {"events": Event.objects.all()})


@staff_required
@require_http_methods(["GET", "POST"])
def event_edit(request, pk=None):
    event = get_object_or_404(Event, pk=pk) if pk else None
    old_auto_assign = event.auto_assign if event else None
    form = EventForm(request.POST or None, instance=event)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            new_auto_assign = form.cleaned_data["auto_assign"]
            if form.cleaned_data["is_active"]:
                Event.objects.filter(is_active=True).exclude(pk=getattr(event, "pk", None)).update(is_active=False)
            saved = form.save(commit=False)
            if event is not None:
                saved.auto_assign = old_auto_assign  # switched below via the service
            saved.save()
            services.audit("event_saved", saved, actor=request.user, via=Via.ADMIN, created=event is None)
            if event is not None and new_auto_assign != old_auto_assign:
                services.set_auto_assign(saved, new_auto_assign, actor=request.user)
        messages.success(request, "Event gespeichert.")
        return redirect("voucher:admin_events")
    return render(request, "voucher/admin/event_form.html", {"form": form, "event": event})
