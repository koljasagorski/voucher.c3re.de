"""Django admin as a fallback only. The regular admin work happens in the /orga/ views."""

from django.contrib import admin

from .models import Assignment, AuditLog, Event, InboundMail, TicketRequest, Voucher


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "phase_start", "phase_end", "auto_assign", "is_active")


@admin.register(TicketRequest)
class TicketRequestAdmin(admin.ModelAdmin):
    list_display = ("user", "event", "position", "status", "updated_at")
    list_filter = ("event", "status")
    search_fields = ("user__email", "user__nickname")


@admin.register(Voucher)
class VoucherAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "source", "status", "parent", "received_at")
    list_filter = ("event", "status", "source")
    raw_id_fields = ("parent", "inbound_mail")


@admin.register(Assignment)
class AssignmentAdmin(admin.ModelAdmin):
    list_display = ("id", "voucher", "ticket_request", "status", "assigned_at", "deadline_at")
    list_filter = ("status",)
    raw_id_fields = ("voucher", "ticket_request")


@admin.register(InboundMail)
class InboundMailAdmin(admin.ModelAdmin):
    list_display = ("id", "received_at", "from_address", "subject", "parse_status")
    list_filter = ("parse_status",)
    raw_id_fields = ("voucher",)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "actor", "via", "action", "target_type", "target_id")
    list_filter = ("via", "action")

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
