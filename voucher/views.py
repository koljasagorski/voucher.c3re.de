from django.http import HttpResponse


def dashboard(request):  # replaced by the user dashboard
    return HttpResponse("")


def action_link(request, token):  # replaced by the mail action view
    return HttpResponse("")


def admin_slots(request):  # replaced by the admin dashboard
    return HttpResponse("")
