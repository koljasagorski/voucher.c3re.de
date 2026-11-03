"""Tiny fixed-window rate limiter on top of the Django cache (shared DB cache in production)."""

import hashlib
import time

from django.conf import settings
from django.core.cache import cache


def client_ip(request):
    if settings.USE_X_FORWARDED_FOR:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            # The right-most entry was added by our own reverse proxy.
            return forwarded.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR", "")


def hit(scope, identifier, limit, window_seconds):
    """Count one attempt. Returns ``False`` if the limit for this window is exceeded.

    The window starts with the first attempt. The entry is always written with an explicit
    timeout: ``cache.incr`` must not be used, because the DB cache implements it as
    get+set with the *default* timeout (300 s), which would shrink every window to 5 min.
    """
    digest = hashlib.sha256(f"{scope}:{identifier}".lower().encode()).hexdigest()[:32]
    key = f"rl:{scope}:{digest}"
    now = time.time()
    entry = cache.get(key)
    if not entry or entry[0] + window_seconds <= now:
        entry = (now, 0)
    start, count = entry[0], entry[1] + 1
    cache.set(key, (start, count), timeout=max(1, int(start + window_seconds - now) + 1))
    return count <= limit


def allowed(request, scope, limits, email=None):
    """Check all limits; ``limits`` is a dict with ``ip`` and optional ``email`` (limit, window)."""
    ok = hit(scope + ":ip", client_ip(request), *limits["ip"])
    if email and "email" in limits:
        ok = hit(scope + ":email", email, *limits["email"]) and ok
    return ok


LIMITS = {
    "register": {"ip": (5, 3600)},
    "magic_link": {"ip": (10, 900), "email": (3, 900)},
    "admin_login": {"ip": (10, 900), "email": (5, 900)},
    "email_change": {"ip": (5, 3600)},
}
