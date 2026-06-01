"""Allocations Engine UI — список правил, создание, preview распределения."""

from datetime import date as date_cls

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from ..access import role_required
from ..models import (
    AllocationRule,
    CashFlowArticle,
    Department,
    Organization,
    UserRole,
)
from ..services.allocations import preview_allocation
from ._shared import parse_optional_int


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def allocations_index(request):
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "create":
                _create_rule(request)
                messages.success(request, "Правило распределения создано")
            elif action == "delete":
                rule = get_object_or_404(
                    AllocationRule, pk=parse_optional_int(request.POST.get("rule_id"))
                )
                name = rule.name
                rule.delete()
                messages.success(request, f"Правило «{name}» удалено")
            elif action == "toggle_active":
                rule = get_object_or_404(
                    AllocationRule, pk=parse_optional_int(request.POST.get("rule_id"))
                )
                rule.is_active = not rule.is_active
                rule.save(update_fields=["is_active"])
                messages.success(
                    request,
                    f"Правило «{rule.name}» {'активировано' if rule.is_active else 'отключено'}",
                )
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect("allocations_index")

    selected_rule_id = parse_optional_int(request.GET.get("preview"))
    today = timezone.localdate()
    period_start_raw = (request.GET.get("period_start") or "").strip()
    period_end_raw = (request.GET.get("period_end") or "").strip()
    try:
        period_start = date_cls.fromisoformat(period_start_raw) if period_start_raw else today.replace(month=1, day=1)
    except ValueError:
        period_start = today.replace(month=1, day=1)
    try:
        period_end = date_cls.fromisoformat(period_end_raw) if period_end_raw else today
    except ValueError:
        period_end = today

    preview = None
    if selected_rule_id:
        rule = AllocationRule.objects.filter(pk=selected_rule_id).select_related(
            "source_article", "organization"
        ).first()
        if rule:
            preview = preview_allocation(rule, period_start, period_end)

    rules = AllocationRule.objects.select_related(
        "organization", "source_article", "created_by"
    ).order_by("organization__name", "name")

    return render(
        request,
        "core/allocations.html",
        {
            "active_section": "settings",
            "rules": rules,
            "organizations": Organization.objects.order_by("name"),
            "articles": CashFlowArticle.objects.order_by("code"),
            "departments": Department.objects.order_by("code"),
            "preview": preview,
            "selected_rule_id": selected_rule_id,
            "period_start": period_start,
            "period_end": period_end,
            "today": today,
        },
    )


def _create_rule(request) -> AllocationRule:
    name = (request.POST.get("name") or "").strip()
    if not name:
        raise ValueError("Укажите название правила")
    organization = get_object_or_404(
        Organization, pk=parse_optional_int(request.POST.get("organization_id"))
    )
    source_article = get_object_or_404(
        CashFlowArticle, pk=parse_optional_int(request.POST.get("source_article_id"))
    )
    method = request.POST.get("method") or AllocationRule.METHOD_PERCENT
    if method not in {m for m, _ in AllocationRule.METHOD_CHOICES}:
        method = AllocationRule.METHOD_PERCENT

    # target_pct из формы — собираем все target_dept_{id} → pct_{id}
    target_pct: dict[str, int] = {}
    for key, value in request.POST.items():
        if not key.startswith("pct_"):
            continue
        dept_id = key.removeprefix("pct_")
        try:
            pct = int(value)
        except (TypeError, ValueError):
            continue
        if pct > 0:
            target_pct[dept_id] = pct

    if not target_pct:
        raise ValueError("Укажите проценты хотя бы для одного ЦФО")

    return AllocationRule.objects.create(
        name=name,
        organization=organization,
        source_article=source_article,
        method=method,
        target_pct=target_pct,
        comment=(request.POST.get("comment") or "").strip(),
        created_by=request.user,
    )
