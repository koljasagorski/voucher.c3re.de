from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods, require_POST
from django_otp import login as otp_login
from django_otp import match_token

from voucher.models import AuditLog, TicketRequest
from voucher.services import audit

from . import mails, ratelimit, tokens
from .forms import AdminLoginForm, DeleteAccountForm, EmailForm, RegisterForm
from .models import User

Via = AuditLog.Via
BACKEND = "django.contrib.auth.backends.ModelBackend"


def _limited(request):
    return render(
        request,
        "accounts/message.html",
        {"title": "Zu viele Versuche", "text": "Bitte warte ein paar Minuten und versuche es dann erneut."},
        status=429,
    )


def _invalid_link(request):
    return render(
        request,
        "accounts/message.html",
        {"title": "Link ungültig", "text": "Der Link ist abgelaufen, wurde schon benutzt oder ist ungültig."},
        status=400,
    )


def _check_mail(request, text):
    return render(request, "accounts/message.html", {"title": "Schau in dein Postfach", "text": text})


def _confirm(request, title, text, button):
    return render(request, "accounts/confirm.html", {"title": title, "text": text, "button": button})


# --- registration ------------------------------------------------------------------


@require_http_methods(["GET", "POST"])
def register(request):
    if request.user.is_authenticated:
        return redirect("voucher:dashboard")
    form = RegisterForm(request.POST or None)
    if request.method == "POST":
        if not ratelimit.allowed(request, "register", ratelimit.LIMITS["register"]):
            return _limited(request)
        if form.is_valid():
            email = form.cleaned_data["email"]
            user = User.objects.filter(email=email).first()
            if user is None:
                user = User.objects.create_user(email=email, nickname=form.cleaned_data["nickname"])
                audit("register", user, actor=user, via=Via.DASHBOARD)
                mails.verify_email(user)
            elif not user.email_verified:
                mails.verify_email(user)
            # Same answer whether or not the address is known.
            return _check_mail(
                request,
                "Wir haben dir eine Mail geschickt. Bitte bestätige darin deine E-Mail-Adresse. "
                "Danach schaltet dich ein Admin frei.",
            )
    return render(
        request,
        "accounts/form.html",
        {"title": "Registrieren", "form": form, "button": "Registrieren",
         "intro": "Du brauchst nur einen Nickname und eine E-Mail-Adresse. Kein Passwort."},
    )


@require_http_methods(["GET", "POST"])
def verify_email(request, token):
    user, email = tokens.resolve_email_token(token, tokens.VERIFY_SALT)
    if user is None or email != user.email:
        return _invalid_link(request)
    if user.email_verified:
        return render(request, "accounts/message.html", {"title": "Schon bestätigt", "text": "Deine E-Mail-Adresse ist bereits bestätigt."})
    if request.method == "GET":
        return _confirm(request, "E-Mail-Adresse bestätigen", f"Adresse {email} bestätigen?", "Bestätigen")
    user.email_verified = True
    user.save(update_fields=["email_verified"])
    audit("email_verified", user, actor=user, via=Via.MAIL_LINK)
    return render(
        request,
        "accounts/message.html",
        {"title": "Danke!", "text": "Deine E-Mail-Adresse ist bestätigt. Sobald ein Admin dich freischaltet, bekommst du eine Mail."},
    )


# --- magic link login ---------------------------------------------------------------


@require_http_methods(["GET", "POST"])
def login_request(request):
    if request.user.is_authenticated:
        return redirect("voucher:dashboard")
    form = EmailForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        if not ratelimit.allowed(request, "magic_link", ratelimit.LIMITS["magic_link"], email=email):
            return _limited(request)
        user = User.objects.filter(email=email).first()
        if user is not None and user.can_login:
            mails.magic_link(user)
        return _check_mail(
            request,
            "Wenn die Adresse zu einem freigeschalteten Account gehört, haben wir dir einen "
            "Anmeldelink geschickt. Er ist 15 Minuten gültig.",
        )
    return render(
        request,
        "accounts/form.html",
        {"title": "Anmelden", "form": form, "button": "Anmeldelink schicken",
         "intro": "Wir schicken dir einen Anmeldelink per Mail."},
    )


@require_http_methods(["GET", "POST"])
def magic_login(request, token):
    user = tokens.resolve_magic_token(token)
    if user is None:
        return _invalid_link(request)
    if request.method == "GET":
        return _confirm(request, "Anmelden", f"Als {user.nickname} anmelden?", "Anmelden")
    login(request, user, backend=BACKEND)  # updates last_login -> link is used up
    audit("login", user, actor=user, via=Via.MAIL_LINK)
    return redirect("voucher:dashboard")


# --- admin login ------------------------------------------------------------------


@require_http_methods(["GET", "POST"])
def admin_login(request):
    form = AdminLoginForm(request.POST or None)
    if request.method == "POST":
        email = (request.POST.get("email") or "").strip().lower()
        if not ratelimit.allowed(request, "admin_login", ratelimit.LIMITS["admin_login"], email=email):
            return _limited(request)
        if form.is_valid():
            user = authenticate(request, username=email, password=form.cleaned_data["password"])
            device = match_token(user, form.cleaned_data["otp_token"].strip()) if user else None
            if user is not None and user.is_staff and device is not None:
                login(request, user, backend=BACKEND)
                otp_login(request, device)
                audit("admin_login", user, actor=user, via=Via.ADMIN)
                next_url = request.GET.get("next", "")
                if url_has_allowed_host_and_scheme(next_url, {request.get_host()}, request.is_secure()):
                    return redirect(next_url)
                return redirect("voucher:admin_waitlist")
            form.add_error(None, "Anmeldung fehlgeschlagen.")
    return render(request, "accounts/form.html", {"title": "Admin-Anmeldung", "form": form, "button": "Anmelden"})


@require_POST
def logout_view(request):
    logout(request)
    return redirect("accounts:login")


# --- account settings ----------------------------------------------------------------


@login_required
@require_http_methods(["GET", "POST"])
def account_settings(request):
    user = request.user
    email_form = EmailForm(prefix="email")
    delete_form = DeleteAccountForm(prefix="delete")
    if request.method == "POST" and "change_email" in request.POST:
        email_form = EmailForm(request.POST, prefix="email")
        if not ratelimit.allowed(request, "email_change", ratelimit.LIMITS["email_change"]):
            return _limited(request)
        if email_form.is_valid():
            new_email = email_form.cleaned_data["email"]
            if new_email == user.email:
                email_form.add_error("email", "Das ist schon deine Adresse.")
            elif User.objects.filter(email=new_email).exists():
                email_form.add_error("email", "Diese Adresse kann nicht verwendet werden.")
            else:
                mails.email_change(user, new_email)
                messages.success(request, "Wir haben eine Bestätigungsmail an die neue Adresse geschickt.")
                return redirect("accounts:settings")
    elif request.method == "POST" and "delete_account" in request.POST:
        delete_form = DeleteAccountForm(request.POST, prefix="delete")
        if delete_form.is_valid():
            with transaction.atomic():
                # Lock the user's requests so no voucher can be assigned between check and delete.
                requests = list(user.ticket_requests.select_for_update())
                holding = any(r.status in TicketRequest.HOLDING_STATUSES for r in requests)
                if not holding:
                    audit("account_deleted", user, actor=None, via=Via.DASHBOARD)
                    user.delete()
            if holding:
                delete_form.add_error(
                    None,
                    "Du hältst gerade einen Voucher. Schließe den Vorgang erst ab oder gib den Voucher zurück.",
                )
            else:
                logout(request)
                return render(request, "accounts/message.html", {"title": "Account gelöscht", "text": "Dein Account und deine Einträge sind gelöscht."})
    return render(
        request,
        "accounts/settings.html",
        {"email_form": email_form, "delete_form": delete_form},
    )


@require_http_methods(["GET", "POST"])
def confirm_email_change(request, token):
    user, email = tokens.resolve_email_token(token, tokens.CHANGE_SALT)
    if user is None or email == user.email:
        return _invalid_link(request)
    if request.method == "GET":
        return _confirm(request, "Neue E-Mail-Adresse bestätigen", f"Deine Adresse auf {email} ändern?", "Ändern")
    if User.objects.filter(email=email).exclude(pk=user.pk).exists():
        return _invalid_link(request)
    user.email = email
    user.email_verified = True
    user.save(update_fields=["email", "email_verified"])
    audit("email_changed", user, actor=user, via=Via.MAIL_LINK)
    return render(request, "accounts/message.html", {"title": "Erledigt", "text": "Deine E-Mail-Adresse wurde geändert."})
