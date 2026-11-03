from django import forms
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from . import services, tokens
from .models import AuditLog, TicketRequest

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


def dashboard(request):  # replaced by the user dashboard
    return HttpResponse("")


def admin_slots(request):  # replaced by the admin dashboard
    return HttpResponse("")


def admin_mail(request, pk):  # replaced by the admin dashboard
    return HttpResponse("")
