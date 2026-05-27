"""User-facing notifications list + mark-as-read action."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from ..models import Notification
from ..services.notifications import mark_read
from ._shared import parse_optional_int


PAGE_SIZE = 30


@login_required
def notifications(request):
    """Show the user's notifications. POST actions: mark_read, mark_all_read."""
    if request.method == "POST":
        # `notif_action` is sent by the global header dropdown; the full page
        # uses `action`. Accept either so both surfaces share this view.
        action = request.POST.get("action") or request.POST.get("notif_action")
        if action == "mark_all_read":
            count = mark_read(user=request.user)
            if count:
                messages.success(request, f"Помечено как прочитанное: {count}")
        elif action == "mark_read":
            ids = request.POST.getlist("notification_id")
            mark_read(user=request.user, notification_ids=ids)
        next_url = request.POST.get("next") or request.path
        if not url_has_allowed_host_and_scheme(next_url, {request.get_host()}):
            next_url = "/notifications/"
        return redirect(next_url)

    show_filter = (request.GET.get("filter") or "all").strip()
    if show_filter not in ("all", "unread"):
        show_filter = "all"

    query = Notification.objects.filter(recipient=request.user)
    if show_filter == "unread":
        query = query.filter(read_at__isnull=True)

    paginator = Paginator(query, PAGE_SIZE)
    page = paginator.get_page(parse_optional_int(request.GET.get("page")) or 1)

    return render(
        request,
        "core/notifications.html",
        {
            "active_section": "notifications",
            "page": page,
            "total": paginator.count,
            "unread_total": Notification.objects.filter(recipient=request.user, read_at__isnull=True).count(),
            "show_filter": show_filter,
        },
    )


@login_required
def notification_follow(request, notification_id: int):
    """Mark a single notification as read and redirect to its link."""
    notif = Notification.objects.filter(recipient=request.user, pk=notification_id).first()
    if notif is None:
        return redirect("notifications")
    mark_read(user=request.user, notification_ids=[notif.id])
    target = notif.link or "/notifications/"
    if not url_has_allowed_host_and_scheme(target, {request.get_host()}):
        target = "/notifications/"
    return redirect(target)
