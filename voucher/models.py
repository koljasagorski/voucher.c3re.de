from django.conf import settings
from django.db import models
from django.db.models import Q


class Event(models.Model):
    class MissedDeadlinePolicy(models.TextChoices):
        END_OF_LIST = "end_of_list", "Ans Listenende"
        REMOVE = "remove", "Von der Liste entfernen"

    name = models.CharField("Name", max_length=100)
    slug = models.SlugField(unique=True)
    phase_start = models.DateTimeField("Beginn Voucherphase")
    phase_end = models.DateTimeField("Ende Voucherphase")
    redeem_deadline_hours = models.PositiveIntegerField("Einlösefrist (h)", default=24)
    pay_reminder_hours = models.PositiveIntegerField("Zahlungserinnerung nach (h)", default=24)
    auto_assign = models.BooleanField("Automatisch zuweisen", default=True)
    missed_deadline_policy = models.CharField(
        "Bei verpasster Frist",
        max_length=20,
        choices=MissedDeadlinePolicy.choices,
        default=MissedDeadlinePolicy.END_OF_LIST,
    )
    is_active = models.BooleanField("Aktiv", default=False)

    class Meta:
        ordering = ["-phase_start"]
        constraints = [
            models.UniqueConstraint(
                fields=["is_active"], condition=Q(is_active=True), name="only_one_active_event"
            )
        ]

    def __str__(self):
        return self.name


class TicketRequest(models.Model):
    class Status(models.TextChoices):
        WAITING = "waiting", "Wartet"
        PAUSED = "paused", "Pausiert"
        ASSIGNED = "assigned", "Voucher zugewiesen"
        REDEEMED = "redeemed", "Eingelöst"
        PAID = "paid", "Bezahlt"
        COMPLETED = "completed", "Abgeschlossen"
        WITHDRAWN = "withdrawn", "Zurückgezogen"
        REMOVED = "removed", "Entfernt"

    OPEN_STATUSES = (Status.WAITING, Status.PAUSED)
    HOLDING_STATUSES = (Status.ASSIGNED, Status.REDEEMED, Status.PAID)

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="requests")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ticket_requests"
    )
    position = models.IntegerField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.WAITING)
    admin_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["event", "user"], name="one_request_per_user_and_event")
        ]
        indexes = [models.Index(fields=["event", "status", "position"])]

    def __str__(self):
        return f"{self.user} @ {self.event} (#{self.position}, {self.status})"


class Voucher(models.Model):
    class Source(models.TextChoices):
        INITIAL = "initial", "Start-Voucher"
        REPLICATED = "replicated", "Folgevoucher"

    class Status(models.TextChoices):
        RECEIVED = "received", "Frei"
        ASSIGNED = "assigned", "Zugewiesen"
        REDEEMED = "redeemed", "Eingelöst"
        PAID = "paid", "Bezahlt"
        REPLICATED = "replicated", "Repliziert"
        INVALID = "invalid", "Ungültig"

    IN_CIRCULATION = (Status.RECEIVED, Status.ASSIGNED, Status.REDEEMED, Status.PAID)

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="vouchers")
    code = models.CharField(max_length=128, unique=True)
    source = models.CharField(max_length=20, choices=Source.choices)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children"
    )
    inbound_mail = models.ForeignKey(
        "InboundMail", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    received_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RECEIVED)

    class Meta:
        ordering = ["received_at", "id"]

    def __str__(self):
        # Never expose the code in string representations (logs, admin titles).
        return f"Voucher #{self.pk} ({self.get_status_display()})"


class Assignment(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Aktiv"
        COMPLETED = "completed", "Abgeschlossen"
        REVOKED = "revoked", "Aufgehoben"
        EXPIRED = "expired", "Abgelaufen"

    voucher = models.ForeignKey(Voucher, on_delete=models.CASCADE, related_name="assignments")
    ticket_request = models.ForeignKey(
        TicketRequest, on_delete=models.CASCADE, related_name="assignments"
    )
    assigned_at = models.DateTimeField()
    deadline_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    redeemed_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    order_number = models.CharField(max_length=64, blank=True)
    revoke_reason = models.CharField(max_length=200, blank=True)
    # Bookkeeping so periodic reminders are sent exactly once / once per day.
    redeem_reminder_sent_at = models.DateTimeField(null=True, blank=True)
    pay_reminder_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-assigned_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["voucher"], condition=Q(status="active"), name="one_active_assignment_per_voucher"
            ),
            models.UniqueConstraint(
                fields=["ticket_request"],
                condition=Q(status="active"),
                name="one_active_assignment_per_request",
            ),
        ]

    def __str__(self):
        return f"Assignment #{self.pk} ({self.status})"

    @property
    def last_action_at(self):
        return max(t for t in (self.assigned_at, self.redeemed_at, self.paid_at) if t)


class InboundMail(models.Model):
    class ParseStatus(models.TextChoices):
        PARSED = "parsed", "Verarbeitet"
        NO_CODE = "no_code", "Kein Code"
        REJECTED_SENDER = "rejected_sender", "Absender abgelehnt"
        DUPLICATE = "duplicate", "Duplikat"
        QUARANTINE = "quarantine", "Quarantäne"
        DISCARDED = "discarded", "Verworfen"

    received_at = models.DateTimeField()
    message_id = models.CharField(max_length=512, unique=True)
    from_address = models.CharField(max_length=320)
    subject = models.CharField(max_length=500, blank=True)
    raw = models.TextField()
    auth_results = models.TextField(blank=True)
    parse_status = models.CharField(max_length=20, choices=ParseStatus.choices)
    voucher = models.ForeignKey(
        Voucher, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    admin_note = models.TextField(blank=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return f"Mail #{self.pk} ({self.parse_status})"


class AuditLog(models.Model):
    class Via(models.TextChoices):
        DASHBOARD = "dashboard", "Dashboard"
        MAIL_LINK = "mail_link", "Mail-Link"
        ADMIN = "admin", "Admin"
        SYSTEM = "system", "System"

    timestamp = models.DateTimeField(auto_now_add=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    via = models.CharField(max_length=20, choices=Via.choices)
    action = models.CharField(max_length=64)
    target_type = models.CharField(max_length=64)
    target_id = models.BigIntegerField(null=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-timestamp", "-id"]

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M} {self.action} {self.target_type}#{self.target_id}"
