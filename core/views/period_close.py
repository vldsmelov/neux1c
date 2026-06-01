"""Страница закрытия учётных периодов. Доступна администраторам
и руководителям (финансовый директор)."""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from ..access import role_required
from ..models import AccountingPeriodLock, Organization, UserRole
from ..services.period_close import (
    close_period,
    organization_lock_summary,
    reopen_period,
)
from ._shared import parse_optional_int, working_organization


@role_required(UserRole.ADMINISTRATOR, UserRole.MANAGER)
def period_close(request):
    org = working_organization(request)
    selected_org_id = parse_optional_int(request.GET.get("organization_id")) or (org.id if org else None)

    if request.method == "POST":
        action = request.POST.get("action")
        org_obj = get_object_or_404(
            Organization,
            pk=parse_optional_int(request.POST.get("organization_id")) or selected_org_id,
        )
        try:
            if action == "close":
                year = int(request.POST.get("year"))
                month = int(request.POST.get("month"))
                comment = (request.POST.get("comment") or "").strip()
                close_period(
                    organization=org_obj,
                    year=year,
                    month=month,
                    user=request.user,
                    comment=comment,
                )
                messages.success(
                    request,
                    f"Период {year}-{month:02d} закрыт для {org_obj.name}",
                )
            elif action == "reopen":
                lock = get_object_or_404(
                    AccountingPeriodLock,
                    pk=parse_optional_int(request.POST.get("lock_id")),
                )
                reason = (request.POST.get("reason") or "").strip()
                reopen_period(lock, request.user, reason)
                messages.success(
                    request,
                    f"Период {lock.year}-{lock.month:02d} открыт для правок ({reason})",
                )
        except (ValueError, KeyError) as exc:
            messages.error(request, str(exc))
        return redirect(f"/settings/period-close/?organization_id={org_obj.id}")

    selected_org = (
        Organization.objects.filter(pk=selected_org_id).first()
        if selected_org_id else None
    )
    rows = organization_lock_summary(selected_org) if selected_org else []

    return render(
        request,
        "core/period_close.html",
        {
            "active_section": "settings",
            "organizations": Organization.objects.order_by("name"),
            "selected_org": selected_org,
            "selected_org_id": selected_org_id,
            "rows": rows,
        },
    )
