from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ("email",)
    list_display = ("email", "nickname", "email_verified", "approved", "is_staff", "is_active")
    list_filter = ("approved", "email_verified", "is_staff", "is_active")
    search_fields = ("email", "nickname")
    fieldsets = (
        (None, {"fields": ("email", "nickname", "password")}),
        ("Status", {"fields": ("email_verified", "approved", "is_active", "is_staff", "is_superuser")}),
        ("Daten", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("email", "nickname", "password1", "password2")}),)
