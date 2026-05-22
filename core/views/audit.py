"""Audit log browser — admin-only journal of recorded user actions."""

from datetime import datetime, time

from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render

from ..access import role_required
from ..models import AuditAction, AuditLog, UserRole
from ._shared import parse_optional_date, parse_optional_int


PAGE_SIZE = 50


@role_required(UserRole.ADMINISTRATOR)
def audit_log(request):
    """List audit log entries with filters: user, action, date range, free-text."""
    query = AuditLog.objects.select_related("user").all()

    selected_user_id = parse_optional_int(request.GET.get("user_id"))
    selected_action = (request.GET.get("action") or "").strip()
    if selected_action and selected_action not in AuditAction.values:
        selected_action = ""
    selected_date_from = parse_optional_date(request.GET.get("date_from"))
    selected_date_to = parse_optional_date(request.GET.get("date_to"))
    search_text = (request.GET.get("q") or "").strip()

    if selected_user_id:
        query = query.filter(user_id=selected_user_id)
    if selected_action:
        query = query.filter(action=selected_action)
    if selected_date_from:
        query = query.filter(created_at__gte=datetime.combine(selected_date_from, time.min))
    if selected_date_to:
        query = query.filter(created_at__lte=datetime.combine(selected_date_to, time.max))
    if search_text:
        query = query.filter(
            Q(path__icontains=search_text)
            | Q(message__icontains=search_text)
            | Q(object_type__icontains=search_text)
            | Q(object_id__iexact=search_text)
        )

    paginator = Paginator(query, PAGE_SIZE)
    page_number = parse_optional_int(request.GET.get("page")) or 1
    page = paginator.get_page(page_number)

    # Preserve filters in pagination links
    base_qs = request.GET.copy()
    base_qs.pop("page", None)
    base_query = base_qs.urlencode()

    User = get_user_model()
    return render(
        request,
        "core/audit_log.html",
        {
            "active_section": "settings",
            "page": page,
            "total": paginator.count,
            "page_size": PAGE_SIZE,
            "action_choices": [("", "Все действия")] + list(AuditAction.choices),
            "users": User.objects.order_by("username"),
            "selected_user_id": selected_user_id,
            "selected_action": selected_action,
            "selected_date_from": request.GET.get("date_from", ""),
            "selected_date_to": request.GET.get("date_to", ""),
            "search_text": search_text,
            "base_query": base_query,
        },
    )
