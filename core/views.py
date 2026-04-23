from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.views import LoginView
from django.db import connection
from django.db.models import Count, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .models import (
    AdditionalAgreement,
    BudgetLimitAdjustment,
    BudgetLimitPlan,
    BudgetPlanStatus,
    CashFlowArticle,
    Contract,
    ContractKind,
    Counterparty,
    Currency,
    Department,
    Nomenclature,
    Organization,
    PaymentFact,
    SyncRun,
    UserRole,
)
from .access import external_accounting_required, role_required
from .services.budget_planning import (
    approve_budget_plan,
    approve_limit_adjustment,
    create_budget_plan,
    create_limit_adjustment,
    submit_budget_plan,
    submit_limit_adjustment,
)
from .services.contract_reservations import build_contract_reservation_rows, build_contract_reservation_summary
from .services.external_accounting import paid_amount_for_contract, pay_contract, remaining_contract_amount


User = get_user_model()


class RoleAwareLoginView(LoginView):
    template_name = "registration/login.html"

    def get_success_url(self):
        profile = getattr(self.request.user, "profile", None)
        if profile and profile.role == UserRole.ACCOUNTANT:
            return reverse("external_accounting")
        return reverse("workspace")


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def workspace(request):
    modules = [
        {
            "name": "Планирование",
            "document": "План / лимит",
            "state": "В работе",
            "check": "Создание и утверждение",
        },
        {
            "name": "НСИ",
            "document": "Синхронизация Mock-1C",
            "state": "Готово",
            "check": "Данные загружены",
        },
        {
            "name": "Договоры",
            "document": "Дерево договоров",
            "state": "НСИ готова",
            "check": "Резерв считается",
        },
        {
            "name": "Отчетность",
            "document": "План-факт БДДС",
            "state": "Ожидает лимиты",
            "check": "Макет ТЗ",
        },
    ]
    counts = {
        "articles": CashFlowArticle.objects.count(),
        "contracts": Contract.objects.count(),
        "payment_facts": PaymentFact.objects.count(),
        "sync_runs": SyncRun.objects.count(),
    }
    return render(
        request,
        "core/workspace.html",
        {
            "modules": modules,
            "counts": counts,
            "active_section": "workspace",
        },
    )


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST)
def nsi_dashboard(request):
    article_stats = CashFlowArticle.objects.aggregate(
        total=Count("id"),
        missing_in_one_c=Count("id", filter=Q(exists_in_one_c=False)),
        internal_turnover=Count("id", filter=Q(is_internal_turnover=True)),
    )
    facts_total = PaymentFact.objects.aggregate(total=Sum("amount"))["total"] or 0
    supplier_contracts = Contract.objects.filter(kind=ContractKind.SOLE_SUPPLIER).prefetch_related(
        "additional_agreements",
        "counterparty",
        "currency",
    )

    context = {
        "latest_sync": SyncRun.objects.first(),
        "counts": {
            "organizations": Organization.objects.count(),
            "departments": Department.objects.count(),
            "currencies": Currency.objects.count(),
            "articles": article_stats["total"],
            "counterparties": Counterparty.objects.count(),
            "nomenclature": Nomenclature.objects.count(),
            "contracts": Contract.objects.count(),
            "agreements": AdditionalAgreement.objects.count(),
            "payment_facts": PaymentFact.objects.count(),
        },
        "article_stats": article_stats,
        "facts_total": facts_total,
        "articles": CashFlowArticle.objects.all()[:20],
        "supplier_contracts": supplier_contracts,
        "payment_facts": PaymentFact.objects.select_related("article", "counterparty", "currency").all()[:20],
        "active_section": "nsi",
    }
    return render(request, "core/nsi_dashboard.html", context)


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def planning_limits(request):
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "create":
                if not _can_edit_plans(request.user):
                    raise ValueError("Создавать планы может только экономист или администратор")
                create_budget_plan(
                    author=request.user,
                    department=get_object_or_404(Department, pk=request.POST.get("department_id")),
                    article=get_object_or_404(CashFlowArticle, pk=request.POST.get("article_id")),
                    currency=get_object_or_404(Currency, pk=request.POST.get("currency_id")),
                    planning_year=int(request.POST.get("planning_year")),
                    planning_horizon=int(request.POST.get("planning_horizon")),
                    annual_amount=_parse_decimal(request.POST.get("annual_amount")),
                    approver=get_object_or_404(User, pk=request.POST.get("approver_id")),
                    monthly_amounts=_parse_monthly_amounts(request.POST),
                    comment=request.POST.get("comment", ""),
                )
                messages.success(request, "План создан")
            elif action == "submit":
                if not _can_edit_plans(request.user):
                    raise ValueError("Отправлять планы может только экономист или администратор")
                submit_budget_plan(get_object_or_404(BudgetLimitPlan, pk=request.POST.get("plan_id")), request.user)
                messages.success(request, "План отправлен на утверждение")
            elif action == "approve":
                approve_budget_plan(get_object_or_404(BudgetLimitPlan, pk=request.POST.get("plan_id")), request.user)
                messages.success(request, "План утвержден")
            elif action == "create_adjustment":
                if not _can_edit_plans(request.user):
                    raise ValueError("Создавать корректировки может только экономист или администратор")
                create_limit_adjustment(
                    author=request.user,
                    base_plan=get_object_or_404(BudgetLimitPlan, pk=request.POST.get("base_plan_id")),
                    new_annual_amount=_parse_decimal(request.POST.get("new_annual_amount")),
                    reason=request.POST.get("reason", ""),
                    monthly_amounts=_parse_adjustment_monthly_amounts(request.POST),
                )
                messages.success(request, "Корректировка создана")
            elif action == "submit_adjustment":
                if not _can_edit_plans(request.user):
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

    context = {
        "active_section": "planning",
        "plans": BudgetLimitPlan.objects.select_related("department", "article", "currency", "approver", "author"),
        "adjustments": BudgetLimitAdjustment.objects.select_related("base_plan", "article", "approver", "author"),
        "approved_plans": BudgetLimitPlan.objects.filter(status=BudgetPlanStatus.APPROVED).select_related("currency"),
        "departments": Department.objects.all(),
        "articles": CashFlowArticle.objects.all(),
        "currencies": Currency.objects.all(),
        "approvers": User.objects.filter(profile__role=UserRole.MANAGER),
        "current_year": timezone.localdate().year,
        "status": BudgetPlanStatus,
        "can_edit_plans": _can_edit_plans(request.user),
        "can_approve_plans": _can_approve_plans(request.user),
    }
    return render(request, "core/planning_limits.html", context)


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def contracts_reservations(request):
    rows = build_contract_reservation_rows()
    summary = build_contract_reservation_summary(rows)
    return render(
        request,
        "core/contracts_reservations.html",
        {
            "active_section": "contracts",
            "rows": rows,
            "summary": summary,
        },
    )


@external_accounting_required
def external_accounting(request):
    if request.method == "POST":
        contract = get_object_or_404(
            Contract,
            pk=request.POST.get("contract_id"),
            kind=ContractKind.SOLE_SUPPLIER,
        )
        try:
            amount = _parse_decimal(request.POST.get("amount")) or remaining_contract_amount(contract)
            payment = pay_contract(contract=contract, accountant=request.user, amount=amount)
            messages.success(request, f"Оплата {payment.number} проведена")
        except (InvalidOperation, ValueError) as exc:
            messages.error(request, str(exc))
        return redirect("external_accounting")

    supplier_contracts = Contract.objects.filter(kind=ContractKind.SOLE_SUPPLIER).select_related(
        "counterparty",
        "currency",
    )
    rows = []
    for contract in supplier_contracts:
        paid_amount = paid_amount_for_contract(contract)
        remaining_amount = remaining_contract_amount(contract)
        rows.append(
            {
                "contract": contract,
                "reserved_amount": contract.reserved_amount,
                "paid_amount": paid_amount,
                "remaining_amount": remaining_amount,
            }
        )

    return render(
        request,
        "core/external_accounting.html",
        {
            "active_section": "external_accounting",
            "rows": rows,
            "payments_total": sum(row["paid_amount"] for row in rows),
        },
    )


def healthz(request):
    database = "ok"
    try:
        connection.ensure_connection()
    except Exception:
        database = "error"

    status = 200 if database == "ok" else 503
    return JsonResponse({"status": "ok" if status == 200 else "error", "database": database}, status=status)


def _parse_decimal(raw_value):
    if not raw_value:
        return None
    return Decimal(raw_value.replace(" ", "").replace(",", "."))


def _parse_monthly_amounts(post_data):
    values = {}
    for month in range(1, 13):
        raw_value = post_data.get(f"month_{month}")
        if raw_value in (None, ""):
            return None
        values[month] = _parse_decimal(raw_value)
    return values


def _parse_adjustment_monthly_amounts(post_data):
    values = {}
    for month in range(1, 13):
        raw_value = post_data.get(f"adjustment_month_{month}")
        if raw_value in (None, ""):
            return None
        values[month] = _parse_decimal(raw_value)
    return values


def _can_edit_plans(user) -> bool:
    if user.is_superuser:
        return True
    profile = getattr(user, "profile", None)
    return bool(profile and profile.role in [UserRole.ADMINISTRATOR, UserRole.ECONOMIST])


def _can_approve_plans(user) -> bool:
    if user.is_superuser:
        return True
    profile = getattr(user, "profile", None)
    return bool(profile and profile.role in [UserRole.ADMINISTRATOR, UserRole.MANAGER])
