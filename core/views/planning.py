"""Planning domain: budgets, limits, primary input wizard, limit adjustments."""

import csv
from decimal import InvalidOperation
from io import StringIO

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from ..access import role_required
from ..models import (
    BudgetLimitAdjustment,
    BudgetLimitPlan,
    BudgetPeriodicity,
    BudgetPlan,
    BudgetPlanStatus,
    BudgetScope,
    CashFlowArticle,
    Currency,
    Department,
    UserRole,
)
from ..services.budget_planning import (
    approve_budget_plan,
    approve_limit_adjustment,
    create_budget_plan,
    create_limit_adjustment,
    create_limit_adjustment_request,
    submit_budget_plan,
    submit_limit_adjustment,
)
from ..services.budgets import (
    approve_budget,
    build_budget_rows,
    build_budget_summary,
    create_budget,
    create_primary_budget_package,
)
from ._permissions import (
    can_approve_budgets,
    can_approve_plans,
    can_create_budgets,
    can_create_limit_adjustments,
    can_create_plans,
    can_delete_plans,
    can_edit_limit_adjustments,
    can_edit_plans,
)
from ._shared import (
    has_model_permission,
    parse_decimal,
    parse_optional_int,
    working_organization,
)


User = get_user_model()
PRIMARY_WIZARD_LIMIT_ROWS = 12
WIZARD_MONTHS = [
    (1, "Янв"), (2, "Фев"), (3, "Мар"), (4, "Апр"),
    (5, "Май"), (6, "Июн"), (7, "Июл"), (8, "Авг"),
    (9, "Сен"), (10, "Окт"), (11, "Ноя"), (12, "Дек"),
]


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def planning_wizard(request):
    departments = list(Department.objects.all())
    org = working_organization(request)
    can_create_package = (
        can_create_budgets(request.user)
        and can_create_plans(request.user)
        and can_edit_plans(request.user)
    )

    submitted: dict = {}
    if request.method == "POST":
        try:
            if not can_create_package:
                raise ValueError(
                    "Первичный ввод бюджета и лимитов может выполнять экономист или администратор"
                )
            budget, limits = create_primary_budget_package(
                author=request.user,
                organization=org,
                budget_year=int(request.POST.get("budget_year")),
                scope=request.POST.get("scope", ""),
                currency=get_object_or_404(Currency, pk=request.POST.get("currency_id")),
                approver=get_object_or_404(User, pk=request.POST.get("approver_id")),
                total_amount=parse_decimal(request.POST.get("total_amount")),
                department_amounts=_parse_department_budget_amounts(request.POST, departments),
                limit_rows=_parse_primary_wizard_limit_rows(request.POST),
                comment=request.POST.get("comment", ""),
            )
            messages.success(
                request,
                f"Документ {budget.number} отправлен на утверждение. Лимитов в пакете: {len(limits)}",
            )
            return redirect("planning_budgets")
        except (InvalidOperation, TypeError, ValueError) as exc:
            messages.error(request, str(exc))
            submitted = request.POST

    return render(
        request,
        "core/planning_wizard.html",
        {
            "active_section": "planning",
            "departments": departments,
            "articles": CashFlowArticle.objects.all(),
            "currencies": Currency.objects.all(),
            "approvers": User.objects.filter(profile__role=UserRole.MANAGER),
            "current_year": timezone.localdate().year,
            "working_organization": org,
            "scope": BudgetScope,
            "periodicity": BudgetPeriodicity,
            "wizard_limit_rows": [
                {
                    "index": index,
                    "hidden": index > 3 and not _planning_wizard_row_has_input(submitted, index),
                }
                for index in range(1, PRIMARY_WIZARD_LIMIT_ROWS + 1)
            ],
            "months": [{"number": number, "label": label} for number, label in WIZARD_MONTHS],
            "can_create_package": can_create_package,
            "submitted": submitted,
        },
    )


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def planning_budgets(request):
    departments = list(Department.objects.all())
    org = working_organization(request)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "create":
                if not can_create_budgets(request.user):
                    raise ValueError("Создавать бюджеты может только экономист или администратор")
                create_budget(
                    author=request.user,
                    organization=org,
                    budget_year=int(request.POST.get("budget_year")),
                    scope=request.POST.get("scope", ""),
                    currency=get_object_or_404(Currency, pk=request.POST.get("currency_id")),
                    total_amount=parse_decimal(request.POST.get("total_amount")),
                    department_amounts=_parse_department_budget_amounts(request.POST, departments),
                    comment=request.POST.get("comment", ""),
                )
                messages.success(request, "Бюджет создан")
            elif action == "approve":
                if not can_approve_budgets(request.user):
                    raise ValueError("Утверждать бюджеты может руководитель или администратор")
                approve_budget(get_object_or_404(BudgetPlan, pk=request.POST.get("budget_id")), request.user)
                messages.success(request, "Бюджет утвержден")
        except (InvalidOperation, ValueError) as exc:
            messages.error(request, str(exc))
        return redirect("planning_budgets")

    rows = build_budget_rows(organization=org)
    return render(
        request,
        "core/planning_budgets.html",
        {
            "active_section": "planning",
            "rows": rows,
            "summary": build_budget_summary(rows),
            "departments": departments,
            "currencies": Currency.objects.all(),
            "current_year": timezone.localdate().year,
            "working_organization": org,
            "scope": BudgetScope,
            "periodicity": BudgetPeriodicity,
            "status": BudgetPlanStatus,
            "can_create_budgets": can_create_budgets(request.user),
            "can_approve_budgets": can_approve_budgets(request.user),
        },
    )


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def planning_limits(request):
    org = working_organization(request)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "create":
                if not can_create_plans(request.user):
                    raise ValueError("Создавать планы может только экономист или администратор")
                budget = None
                budget_id = request.POST.get("budget_id")
                if budget_id:
                    budget = get_object_or_404(BudgetPlan, pk=budget_id)
                create_budget_plan(
                    author=request.user,
                    budget=budget,
                    organization=org,
                    department=get_object_or_404(Department, pk=request.POST.get("department_id")),
                    article=get_object_or_404(CashFlowArticle, pk=request.POST.get("article_id")),
                    currency=budget.currency if budget else get_object_or_404(Currency, pk=request.POST.get("currency_id")),
                    planning_year=budget.budget_year if budget else int(request.POST.get("planning_year")),
                    planning_horizon=int(request.POST.get("planning_horizon")),
                    annual_amount=parse_decimal(request.POST.get("annual_amount")),
                    approver=get_object_or_404(User, pk=request.POST.get("approver_id")),
                    monthly_amounts=_parse_monthly_amounts(request.POST),
                    comment=request.POST.get("comment", ""),
                )
                messages.success(request, "План создан")
            elif action == "submit":
                if not can_edit_plans(request.user):
                    raise ValueError("Отправлять планы может только экономист или администратор")
                submit_budget_plan(get_object_or_404(BudgetLimitPlan, pk=request.POST.get("plan_id")), request.user)
                messages.success(request, "План отправлен на утверждение")
            elif action == "approve":
                approve_budget_plan(get_object_or_404(BudgetLimitPlan, pk=request.POST.get("plan_id")), request.user)
                messages.success(request, "План утвержден")
            elif action == "create_adjustment":
                if not has_model_permission(request.user, BudgetLimitAdjustment, "add"):
                    raise ValueError("Создавать корректировки может только экономист или администратор")
                create_limit_adjustment(
                    author=request.user,
                    base_plan=get_object_or_404(BudgetLimitPlan, pk=request.POST.get("base_plan_id")),
                    new_annual_amount=parse_decimal(request.POST.get("new_annual_amount")),
                    reason=request.POST.get("reason", ""),
                    monthly_amounts=_parse_adjustment_monthly_amounts(request.POST),
                )
                messages.success(request, "Корректировка создана")
            elif action == "submit_adjustment":
                if not has_model_permission(request.user, BudgetLimitAdjustment, "change"):
                    raise ValueError("Отправлять корректировки может только экономист или администратор")
                submit_limit_adjustment(
                    get_object_or_404(BudgetLimitAdjustment, pk=request.POST.get("adjustment_id")),
                    request.user,
                )
                messages.success(request, "Корректировка отправлена на утверждение")
            elif action == "approve_adjustment":
                approve_limit_adjustment(
                    get_object_or_404(BudgetLimitAdjustment, pk=request.POST.get("adjustment_id")),
                    request.user,
                )
                messages.success(request, "Корректировка утверждена")
        except (InvalidOperation, ValueError) as exc:
            messages.error(request, str(exc))
        return redirect("planning_limits")

    from ..models import PlanningScenario
    plans_qs = BudgetLimitPlan.objects.filter(organization=org).select_related(
        "budget", "department", "article", "currency", "approver", "author", "scenario"
    )
    selected_scenario_id = parse_optional_int(request.GET.get("scenario_id"))
    if selected_scenario_id:
        plans_qs = plans_qs.filter(scenario_id=selected_scenario_id)
    scenarios_for_org = (
        PlanningScenario.objects.filter(organization=org).order_by("-year", "-is_baseline", "name")
        if org
        else PlanningScenario.objects.none()
    )
    if request.GET.get("export") == "csv":
        return _export_limits_csv(plans_qs)

    return render(
        request,
        "core/planning_limits.html",
        {
            "active_section": "planning",
            "plans": plans_qs,
            "adjustments": BudgetLimitAdjustment.objects.filter(base_plan__organization=org).select_related("base_plan", "article", "approver", "author", "target_plan", "target_organization"),
            "approved_plans": BudgetLimitPlan.objects.filter(organization=org, status=BudgetPlanStatus.APPROVED).select_related("currency"),
            "budgets": BudgetPlan.objects.filter(organization=org, status=BudgetPlanStatus.APPROVED).select_related("currency"),
            "departments": Department.objects.all(),
            "articles": CashFlowArticle.objects.all(),
            "currencies": Currency.objects.all(),
            "approvers": User.objects.filter(profile__role=UserRole.MANAGER),
            "current_year": timezone.localdate().year,
            "working_organization": org,
            "status": BudgetPlanStatus,
            "can_edit_plans": can_edit_plans(request.user),
            "can_create_plans": can_create_plans(request.user),
            "can_create_limit_adjustments": can_create_limit_adjustments(request.user),
            "can_approve_plans": can_approve_plans(request.user),
            "scenarios_for_org": scenarios_for_org,
            "selected_scenario_id": selected_scenario_id,
            "plans_acl": {
                "view": has_model_permission(request.user, BudgetLimitPlan, "view"),
                "create": can_create_plans(request.user),
                "edit": can_edit_plans(request.user),
                "delete": can_delete_plans(request.user),
            },
        },
    )


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def budget_limit_history(request, plan_id: int):
    """Render the version history (timeline) of a single BudgetLimitPlan."""
    plan = get_object_or_404(
        BudgetLimitPlan.objects.select_related(
            "organization", "department", "article", "currency", "approver", "author", "budget",
        ),
        pk=plan_id,
    )
    plan_months = {row.month: row.amount for row in plan.months.all()}
    months_table = [
        {"number": number, "label": label, "current": plan_months.get(number, 0)}
        for number, label in WIZARD_MONTHS
    ]
    adjustments = list(
        BudgetLimitAdjustment.objects.filter(base_plan=plan)
        .select_related("author", "approver", "target_organization", "target_plan")
        .prefetch_related("months")
        .order_by("version", "created_at")
    )
    # Build a per-adjustment view of monthly amounts indexed by month for the
    # template (otherwise iterating .months in template is awkward).
    timeline = []
    for adj in adjustments:
        adj_months = {row.month: row.amount for row in adj.months.all()}
        timeline.append({
            "adjustment": adj,
            "months": [
                {"number": number, "label": label, "amount": adj_months.get(number, 0)}
                for number, label in WIZARD_MONTHS
            ],
        })
    return render(
        request,
        "core/budget_limit_history.html",
        {
            "active_section": "planning",
            "plan": plan,
            "months_table": months_table,
            "timeline": timeline,
        },
    )


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def limit_adjustment_wizard(request):
    can_create_adjustment = can_create_limit_adjustments(request.user) and can_edit_limit_adjustments(request.user)
    org = working_organization(request)

    submitted: dict = {}
    if request.method == "POST":
        try:
            if not can_create_adjustment:
                raise ValueError("Заявку на корректировку лимита может создать экономист или администратор")
            adjustment = create_limit_adjustment_request(
                author=request.user,
                base_plan=get_object_or_404(
                    BudgetLimitPlan,
                    pk=request.POST.get("base_plan_id"),
                    status=BudgetPlanStatus.APPROVED,
                ),
                new_annual_amount=parse_decimal(request.POST.get("new_annual_amount")),
                reason=request.POST.get("reason", ""),
                monthly_amounts=_parse_adjustment_monthly_amounts(request.POST),
                target_plan=_resolve_target_plan(request.POST.get("target_plan_id")),
            )
            messages.success(request, f"Заявка {adjustment.number} отправлена на утверждение")
            return redirect("planning_limits")
        except (InvalidOperation, TypeError, ValueError) as exc:
            messages.error(request, str(exc))
            submitted = request.POST

    return render(
        request,
        "core/limit_adjustment_wizard.html",
        {
            "active_section": "planning",
            "approved_plans": BudgetLimitPlan.objects.filter(organization=org, status=BudgetPlanStatus.APPROVED)
            .select_related("budget", "department", "article", "currency", "approver")
            .prefetch_related("months"),
            "target_plans": BudgetLimitPlan.objects.filter(status=BudgetPlanStatus.APPROVED)
            .exclude(organization=org)
            .select_related("organization", "budget", "department", "article", "currency"),
            "months": [{"number": number, "label": label} for number, label in WIZARD_MONTHS],
            "can_create_adjustment": can_create_adjustment,
            "working_organization": org,
            "submitted": submitted,
        },
    )


# --- POST parsers (planning-specific) -------------------------------------

def _parse_monthly_amounts(post_data):
    values = {}
    seen_any = False
    for month in range(1, 13):
        raw_value = post_data.get(f"month_{month}")
        if raw_value in (None, ""):
            continue
        seen_any = True
        values[month] = parse_decimal(raw_value)
    if not seen_any:
        return None
    if set(values.keys()) != set(range(1, 13)):
        raise ValueError("Если указываете помесячную разбивку, заполните все 12 месяцев")
    return values


def _parse_adjustment_monthly_amounts(post_data):
    values = {}
    seen_any = False
    for month in range(1, 13):
        raw_value = post_data.get(f"adjustment_month_{month}")
        if raw_value in (None, ""):
            continue
        seen_any = True
        values[month] = parse_decimal(raw_value)
    if not seen_any:
        return None
    if set(values.keys()) != set(range(1, 13)):
        raise ValueError("Если указываете помесячную корректировку, заполните все 12 месяцев")
    return values


def _resolve_target_plan(raw_plan_id):
    plan_id = parse_optional_int(raw_plan_id)
    if not plan_id:
        return None
    return get_object_or_404(BudgetLimitPlan, pk=plan_id, status=BudgetPlanStatus.APPROVED)


def _parse_department_budget_amounts(post_data, departments):
    values = {}
    for department in departments:
        raw_value = (post_data.get(f"department_budget_{department.id}") or "").strip()
        if not raw_value:
            continue
        values[department.id] = parse_decimal(raw_value)
    return values


def _parse_primary_wizard_limit_rows(post_data):
    rows = []
    for index in range(1, PRIMARY_WIZARD_LIMIT_ROWS + 1):
        department_id = (post_data.get(f"limit_{index}_department_id") or "").strip()
        article_id = (post_data.get(f"limit_{index}_article_id") or "").strip()
        annual_amount_raw = (post_data.get(f"limit_{index}_annual_amount") or "").strip()
        has_months = any(
            (post_data.get(f"limit_{index}_month_{month}") or "").strip()
            for month, _label in WIZARD_MONTHS
        )

        if not any([department_id, article_id, annual_amount_raw, has_months]):
            continue
        if not department_id or not article_id or not annual_amount_raw:
            raise ValueError(f"Заполните ЦФО, статью и годовой лимит в строке {index}")

        rows.append(
            {
                "department": get_object_or_404(Department, pk=department_id),
                "article": get_object_or_404(CashFlowArticle, pk=article_id),
                "annual_amount": parse_decimal(annual_amount_raw),
                "monthly_amounts": _parse_primary_wizard_monthly_amounts(post_data, index),
                "comment": (post_data.get(f"limit_{index}_comment") or "").strip(),
            }
        )

    if not rows:
        raise ValueError("Добавьте минимум один лимит")
    return rows


def _parse_primary_wizard_monthly_amounts(post_data, index: int):
    values = {}
    seen_any = False
    for month, _label in WIZARD_MONTHS:
        raw_value = (post_data.get(f"limit_{index}_month_{month}") or "").strip()
        if not raw_value:
            continue
        seen_any = True
        values[month] = parse_decimal(raw_value)
    if not seen_any:
        return None
    if set(values.keys()) != {month for month, _label in WIZARD_MONTHS}:
        raise ValueError(
            f"Если указываете помесячную разбивку в строке {index}, заполните все 12 месяцев"
        )
    return values


def _planning_wizard_row_has_input(submitted, index: int) -> bool:
    if not submitted:
        return False
    prefix = f"limit_{index}_"
    return any(
        submitted.get(prefix + name)
        for name in ("department_id", "article_id", "annual_amount", "comment")
    ) or any(
        submitted.get(f"limit_{index}_month_{month}") for month in range(1, 13)
    )


def _export_limits_csv(queryset) -> HttpResponse:
    """Export approved-and-draft limit plans as an Excel-friendly CSV
    (';' separator + UTF-8 BOM), matching the payments-journal export."""
    buffer = StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow([
        "Номер", "Бюджет", "Компания", "ЦФО", "Статья", "Год",
        "Годовая сумма", "Валюта", "Версия", "Статус", "Согласующий", "Автор",
    ])
    for plan in queryset.iterator():
        writer.writerow([
            plan.number,
            plan.budget.number if plan.budget_id else "",
            plan.organization.name if plan.organization_id else "",
            plan.department.name if plan.department_id else "",
            f"{plan.article.code} · {plan.article.name}" if plan.article_id else "",
            plan.planning_year,
            f"{plan.annual_amount}",
            plan.currency.code if plan.currency_id else "",
            f"v{plan.version}",
            plan.get_status_display(),
            plan.approver.username if plan.approver_id else "",
            plan.author.username if plan.author_id else "",
        ])
    response = HttpResponse("﻿" + buffer.getvalue(), content_type="text/csv; charset=utf-8")
    stamp = timezone.localdate().strftime("%Y%m%d")
    response["Content-Disposition"] = f'attachment; filename="budget_limits_{stamp}.csv"'
    return response
