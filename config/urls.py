from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path
from django_otp.admin import OTPAdminSite

# Django admin is only a fallback and requires an OTP-verified staff session.
admin.site.__class__ = OTPAdminSite

urlpatterns = [
    path("healthz", lambda request: HttpResponse("ok"), name="healthz"),
    path("django-admin/", admin.site.urls),
    path("", include("voucher.urls")),
]
