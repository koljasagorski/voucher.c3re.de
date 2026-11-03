from django.urls import path

from . import views, views_admin

app_name = "voucher"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("aktion/", views.dashboard_action, name="dashboard_action"),
    path("a/<str:token>/", views.action_link, name="action"),
    # Admin dashboard
    path("orga/", views_admin.waitlist, name="admin_waitlist"),
    path("orga/reihenfolge/", views_admin.reorder, name="admin_reorder"),
    path("orga/notiz/<int:pk>/", views_admin.note, name="admin_note"),
    path("orga/export.csv", views_admin.export_csv, name="admin_export"),
    path("orga/slots/", views_admin.slots, name="admin_slots"),
    path("orga/aktion/", views_admin.action, name="admin_action"),
    path("orga/mails/", views_admin.mails, name="admin_mails"),
    path("orga/mails/<int:pk>/", views_admin.mail_detail, name="admin_mail"),
    path("orga/user/", views_admin.users, name="admin_users"),
    path("orga/audit/", views_admin.audit_log, name="admin_audit"),
    path("orga/events/", views_admin.events, name="admin_events"),
    path("orga/events/neu/", views_admin.event_edit, name="admin_event_new"),
    path("orga/events/<int:pk>/", views_admin.event_edit, name="admin_event_edit"),
]
