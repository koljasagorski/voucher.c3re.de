from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from . import notifications, services, tokens
from .models import Assignment, AuditLog, TicketRequest, Voucher

S = TicketRequest.Status


class OrderNumberForm(forms.Form):
    order_number = forms.CharField(
        required=False, max_length=64, widget=forms.TextInput(attrs={"autocomplete": "off"})
    )


# Holder actions, shared by mail links and the dashboard.
HOLDER_ACTIONS = {
    "redeemed": {
        "label": "Eingelöst",
        "question": "Hast du den Voucher im Ticketshop eingelöst?",
        "button": "Ja, eingelöst",
        "status": S.ASSIGNED,
        "form": True,
        "done": "Danke! Bitte bezahle deine Bestellung jetzt – du bekommst dazu gleich eine Mail.",
    },
    "paid": {
        "label": "Bezahlt",
        "question": "Hast du deine Bestellung bezahlt?",
        "button": "Ja, bezahlt",
        "status": S.REDEEMED,
        "form": True,
        "done": "Danke! Sobald der Folgevoucher eingeht, geben wir ihn weiter und sagen dir Bescheid.",
    },
    "invalid": {
        "label": "Voucher funktioniert nicht",
        "question": "Der Ticketshop akzeptiert den Code nicht (z. B. weil er schon eingelöst wurde)? "
        "Du behältst deinen Platz auf der Warteliste.",
        "button": "Voucher als ungültig melden",
        "status": S.ASSIGNED,
        "form": False,
        "done": "Danke für die Meldung. Du bleibst vorne auf der Liste und bekommst den nächsten freien Voucher.",
    },
    "withdraw": {
        "label": "Brauche kein Ticket mehr",
        "question": "Willst du deinen Platz auf der Warteliste freigeben? "
        "Ein reservierter Voucher geht sofort an die nächste Person.",
        "button": "Platz freigeben",
        "status": S.ASSIGNED,
        "form": False,
        "danger": True,
        "done": "Dein Platz ist freigegeben. Danke, dass du Bescheid gesagt hast!",
    },
}


def run_holder_action(action, assignment, *, actor, via, order_number=None):
    """Execute a holder action. Raises ``services.InvalidTransition`` if nothing to do."""
    if action == "redeemed":
        return services.mark_redeemed(assignment, order_number, actor=actor, via=via)
    if action == "paid":
        return services.mark_paid(assignment, order_number, actor=actor, via=via)
    if action == "invalid":
        return services.report_invalid(assignment, actor=actor, via=via)
    if action == "withdraw":
        return services.withdraw(assignment.ticket_request, actor=actor, via=via)
    raise services.InvalidTransition(action)


def _nothing_to_do(request):
    return render(request, "voucher/action_result.html", {"nothing_to_do": True})


@require_http_methods(["GET", "POST"])
def action_link(request, token):
    """Mail link target. GET only shows a confirmation page; POST performs the action."""
    try:
        assignment, action = tokens.resolve(token)
    except tokens.TokenError:
        return _nothing_to_do(request)
    meta = HOLDER_ACTIONS[action]
    if assignment.ticket_request.status != meta["status"]:
        return _nothing_to_do(request)

    if request.method == "GET":
        return render(
            request,
            "voucher/action_confirm.html",
            {
                **meta,
                "assignment": assignment,
                "event": assignment.ticket_request.event,
                "form": OrderNumberForm() if meta["form"] else None,
            },
        )

    form = OrderNumberForm(request.POST)
    order_number = form.cleaned_data["order_number"] if form.is_valid() else None
    try:
        run_holder_action(
            action,
            assignment,
            actor=assignment.ticket_request.user,
            via=AuditLog.Via.MAIL_LINK,
            order_number=order_number,
        )
    except services.InvalidTransition:
        return _nothing_to_do(request)
    return render(request, "voucher/action_result.html", {"done_text": meta["done"]})


# --- user dashboard ----------------------------------------------------------------


def _holder_buttons(ticket_request):
    """Actions that fit the current state (same set as in the mails)."""
    if ticket_request is None:
        return []
    if ticket_request.status == S.ASSIGNED:
        return ["redeemed", "invalid", "withdraw"]
    if ticket_request.status == S.REDEEMED:
        return ["paid"]
    if ticket_request.status in TicketRequest.OPEN_STATUSES:
        return ["withdraw"]
    return []


@login_required
def dashboard(request):
    event = services.get_active_event()
    ticket_request = assignment = None
    ahead = 0
    in_circulation = 0
    if event:
        ticket_request = TicketRequest.objects.filter(event=event, user=request.user).first()
        in_circulation = event.vouchers.filter(status__in=Voucher.IN_CIRCULATION).count()
        if ticket_request:
            assignment = (
                ticket_request.assignments.select_related("voucher").filter(status=Assignment.Status.ACTIVE).first()
            )
            ahead = (
                TicketRequest.objects.filter(event=event, status=S.WAITING)
                .filter(Q(position__lt=ticket_request.position) | Q(position=ticket_request.position, pk__lt=ticket_request.pk))
                .count()
            )
    history = (
        Assignment.objects.filter(ticket_request__user=request.user)
        .select_related("ticket_request__event")
        .order_by("-assigned_at")
    )
    buttons = [(name, HOLDER_ACTIONS[name]) for name in _holder_buttons(ticket_request)]
    return render(
        request,
        "voucher/dashboard.html",
        {
            "event": event,
            "ticket_request": ticket_request,
            "assignment": assignment,
            "ahead": ahead,
            "in_circulation": in_circulation,
            "history": history,
            "buttons": buttons,
            "can_join": event is not None and (ticket_request is None or ticket_request.status == S.WITHDRAWN),
            "shop_url": settings.VOUCHER_SHOP_URL,
            "inbox_address": settings.VOUCHER_INBOX_ADDRESS,
            "deadline": notifications.fmt_datetime(assignment.deadline_at) if assignment else "",
            "order_form": OrderNumberForm(),
        },
    )


@login_required
@require_POST
def dashboard_action(request):
    action = request.POST.get("action", "")
    event = services.get_active_event()
    try:
        if event is None:
            raise services.InvalidTransition("Kein aktives Event")
        if action == "join":
            services.join_waitlist(event, request.user)
            messages.success(request, "Du stehst jetzt auf der Warteliste.")
            return redirect("voucher:dashboard")
        ticket_request = get_object_or_404(TicketRequest, event=event, user=request.user)
        if action not in _holder_buttons(ticket_request):
            raise services.InvalidTransition("nichts zu tun")
        assignment = ticket_request.assignments.filter(status=Assignment.Status.ACTIVE).first()
        form = OrderNumberForm(request.POST)
        order_number = form.cleaned_data["order_number"] if form.is_valid() else None
        if assignment is None:
            if action != "withdraw":
                raise services.InvalidTransition("nichts zu tun")
            services.withdraw(ticket_request, actor=request.user, via=AuditLog.Via.DASHBOARD)
        else:
            run_holder_action(
                action, assignment, actor=request.user, via=AuditLog.Via.DASHBOARD, order_number=order_number
            )
        messages.success(request, HOLDER_ACTIONS[action]["done"])
    except services.InvalidTransition:
        messages.info(request, "Nichts zu tun – der Stand hat sich schon geändert.")
    return redirect("voucher:dashboard")
