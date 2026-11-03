def outbox(request):
    """Number of unsent mails, shown to admins in the /orga/ area."""
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated and user.is_staff and request.path.startswith("/orga/")):
        return {}
    from .models import OutgoingMail

    return {"unsent_mails": OutgoingMail.objects.filter(attempts__gt=0).count()}
