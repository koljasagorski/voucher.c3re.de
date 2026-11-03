"""Tiny fixed-window rate limiter on top of the Django cache (shared DB cache in production)."""

import hashlib

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
    """Count one attempt. Returns ``False`` if the limit for this window is exceeded."""
    digest = hashlib.sha256(f"{scope}:{identifier}".lower().encode()).hexdigest()[:32]
    key = f"rl:{scope}:{digest}"
    cache.add(key, 0, timeout=window_seconds)
    try:
        count = cache.incr(key)
    except ValueError:  # expired between add and incr
        cache.set(key, 1, timeout=window_seconds)
        count = 1
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
