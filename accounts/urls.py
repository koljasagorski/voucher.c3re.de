from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("registrieren/", views.register, name="register"),
    path("bestaetigen/<str:token>/", views.verify_email, name="verify_email"),
    path("anmelden/", views.login_request, name="login"),
    path("anmelden/<str:token>/", views.magic_login, name="magic_login"),
    path("admin-anmelden/", views.admin_login, name="admin_login"),
    path("abmelden/", views.logout_view, name="logout"),
    path("einstellungen/", views.account_settings, name="settings"),
    path("email-aendern/<str:token>/", views.confirm_email_change, name="confirm_email_change"),
]
