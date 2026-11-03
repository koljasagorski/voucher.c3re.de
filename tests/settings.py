import os

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DEBUG", "1")

from config.settings import *  # noqa: E402,F401,F403

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
SITE_URL = "https://voucher.example.de"
ALLOWED_HOSTS = ["testserver", "voucher.example.de"]
DEFAULT_FROM_EMAIL = "voucher@example.de"
VOUCHER_INBOX_ADDRESS = "voucher@example.de"
ADMIN_EMAILS = ["orga@example.de"]
VOUCHER_ALLOWED_SENDERS = ["tickets.events.ccc.de", "noreply@ccc.de"]
VOUCHER_AUTHSERV_ID = "mx.example.de"
