from django.conf import settings
from django.urls import reverse

from voucher.notifications import message

from . import tokens

PREFIX = "[Voucher-Warteliste]"


def _greeting(user):
    return f"Hallo {user.nickname},"


def verify_email(user):
    message(
        user.email,
        f"{PREFIX} Bitte bestätige deine E-Mail-Adresse",
        [
            "danke für deine Registrierung. Bitte bestätige deine E-Mail-Adresse.",
            "Danach schaltet dich ein Admin frei. Erst dann kannst du dich anmelden.",
            "Wenn du dich nicht registriert hast, ignoriere diese Mail einfach.",
        ],
        greeting=_greeting(user),
        link=tokens.verify_url(user),
        link_label="E-Mail-Adresse bestätigen",
    )


def magic_link(user):
    message(
        user.email,
        f"{PREFIX} Dein Anmeldelink",
        [
            "hier ist dein Anmeldelink. Er ist 15 Minuten gültig und funktioniert nur einmal.",
            "Wenn du keinen Link angefordert hast, ignoriere diese Mail einfach.",
        ],
        greeting=_greeting(user),
        link=tokens.magic_url(user),
        link_label="Anmelden",
    )


def approved(user):
    message(
        user.email,
        f"{PREFIX} Du bist freigeschaltet",
        [
            "ein Admin hat deinen Account freigeschaltet. Du kannst dich jetzt anmelden "
            "und dich für ein Ticket eintragen.",
        ],
        greeting=_greeting(user),
        link=settings.SITE_URL + reverse("accounts:login"),
        link_label="Anmelden",
    )


def email_change(user, new_email):
    message(
        new_email,
        f"{PREFIX} Neue E-Mail-Adresse bestätigen",
        [
            "du möchtest deine E-Mail-Adresse auf diese Adresse ändern. Bitte bestätige das.",
            "Wenn du das nicht warst, ignoriere diese Mail einfach.",
        ],
        greeting=_greeting(user),
        link=tokens.change_url(user, new_email),
        link_label="Neue Adresse bestätigen",
    )
