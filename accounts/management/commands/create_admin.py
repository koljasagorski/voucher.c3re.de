import base64
import getpass

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django_otp.plugins.otp_totp.models import TOTPDevice

from accounts.models import User


class Command(BaseCommand):
    help = "Create or update an admin account with password and a TOTP device."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--nickname", default="")
        parser.add_argument("--reset-totp", action="store_true", help="Replace existing TOTP devices.")
        parser.add_argument("--keep-password", action="store_true", help="Do not ask for a new password.")

    def handle(self, *args, email, nickname, reset_totp, keep_password, **options):
        email = email.strip().lower()
        user = User.objects.filter(email=email).first()
        created = user is None
        if created:
            user = User(email=email, nickname=nickname or email.split("@")[0])
        elif nickname:
            user.nickname = nickname

        if created or not keep_password:
            password = getpass.getpass("Passwort: ")
            if password != getpass.getpass("Passwort wiederholen: "):
                raise CommandError("Passwörter stimmen nicht überein.")
            try:
                validate_password(password, user)
            except ValidationError as exc:
                raise CommandError(" ".join(exc.messages)) from None
            user.set_password(password)

        user.is_staff = True
        user.is_superuser = True
        user.approved = True
        user.email_verified = True
        user.is_active = True
        user.save()

        devices = TOTPDevice.objects.filter(user=user)
        if reset_totp:
            devices.delete()
        if not devices.exists():
            device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
            secret = base64.b32encode(device.bin_key).decode()
            self.stdout.write("TOTP-Gerät angelegt. In der Authenticator-App einrichten:")
            self.stdout.write(f"  URI:    {device.config_url}")
            self.stdout.write(f"  Secret: {secret}")
            self.stdout.write("Diese Angaben werden nicht noch einmal angezeigt.")
        self.stdout.write(self.style.SUCCESS(f"Admin {'angelegt' if created else 'aktualisiert'}: {email}"))
