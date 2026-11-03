from django.urls import path

from . import views

app_name = "voucher"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("a/<str:token>/", views.action_link, name="action"),
    path("orga/slots/", views.admin_slots, name="admin_slots"),
]
