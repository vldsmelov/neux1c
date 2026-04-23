from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.views import LoginView
from django.db import connection
from django.db.models import Count, Max, Q, Sum
from django.http import HttpResponse, JsonResponse
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
    PaymentFactAdjustment,
    PaymentLimitControlMode,
    PaymentRequest,
    PaymentRequestControlSettings,
    PaymentRequestKind,
    PaymentRequestStatus,
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
from .services.contract_tree import ContractTreeFilters, build_contract_tree
from .services.external_accounting import paid_amount_for_contract, pay_contract, remaining_contract_amount
from .services.payment_requests import (
    approve_payment_request,
    create_payment_request,
    submit_payment_request,
    transfer_payment_request_to_do,
)
from .services.payment_fact_adjustments import adjust_payment_fact
from .services.plan_fact_report import PlanFactFilters, build_plan_fact_report, to_csv as plan_fact_to_csv


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
            "name": "Заявки",
            "document": "Заявка на оплату",
            "state": "В работе",
            "check": "Лимит + согласование + передача в 1С:ДО",
        },
        {
            "name": "Отчетность",
            "document": "План-факт БДДС",
            "state": "В работе",
            "check": "БУ / НУ + экспорт",
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


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def contracts_tree(request):
    filters = ContractTreeFilters(
        counterparty_id=_parse_optional_int(request.GET.get("counterparty_id")),
        contract_query=(request.GET.get("contract") or "").strip().lower(),
        date_from=_parse_optional_date(request.GET.get("date_from")),
        date_to=_parse_optional_date(request.GET.get("date_to")),
    )
    rows, summary = build_contract_tree(filters)
    counterparties = Counterparty.objects.filter(contracts__isnull=False).distinct().order_by("name")
    return render(
        request,
        "core/contracts_tree.html",
        {
            "active_section": "contracts",
            "rows": rows,
            "summary": summary,
            "counterparties": counterparties,
            "selected_counterparty_id": filters.counterparty_id,
            "selected_contract": request.GET.get("contract", ""),
            "selected_date_from": request.GET.get("date_from", ""),
            "selected_date_to": request.GET.get("date_to", ""),
        },
    )


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def payment_requests(request):
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "create":
                if not _can_manage_payment_requests(request.user):
                    raise ValueError("Создавать заявки может только экономист или администратор")
                contract = None
                contract_id = request.POST.get("contract_id")
                if contract_id:
                    contract = get_object_or_404(Contract, pk=contract_id)
                create_payment_request(
                    author=request.user,
                    request_kind=request.POST.get("request_kind"),
                    organization=get_object_or_404(Organization, pk=request.POST.get("organization_id")),
                    article=get_object_or_404(CashFlowArticle, pk=request.POST.get("article_id")),
                    counterparty=get_object_or_404(Counterparty, pk=request.POST.get("counterparty_id")),
                    contract=contract,
                    currency=get_object_or_404(Currency, pk=request.POST.get("currency_id")),
                    amount=_parse_decimal(request.POST.get("amount")),
                    manual_exchange_rate=_parse_decimal(request.POST.get("manual_exchange_rate")),
                    approver=get_object_or_404(User, pk=request.POST.get("approver_id")),
                    invoice_number=request.POST.get("invoice_number", ""),
                    comment=request.POST.get("comment", ""),
                )
                messages.success(request, "Заявка создана")
            elif action == "submit":
                if not _can_manage_payment_requests(request.user):
                    raise ValueError("Отправлять заявки может только экономист или администратор")
                submit_payment_request(
                    get_object_or_404(PaymentRequest, pk=request.POST.get("request_id")),
                    request.user,
                )
                messages.success(request, "Заявка отправлена на согласование")
            elif action == "approve":
                approve_payment_request(
                    get_object_or_404(PaymentRequest, pk=request.POST.get("request_id")),
                    request.user,
                )
                messages.success(request, "Заявка согласована")
            elif action == "transfer":
                if not _can_manage_payment_requests(request.user):
                    raise ValueError("Передавать в 1С:ДО может только экономист или администратор")
                transfer_payment_request_to_do(
                    get_object_or_404(PaymentRequest, pk=request.POST.get("request_id")),
                    request.user,
                )
                messages.success(request, "Заявка передана в 1С:ДО")
        except (InvalidOperation, ValueError) as exc:
            messages.error(request, str(exc))
        return redirect("payment_requests")

    limit_settings = PaymentRequestControlSettings.active_or_default()
    context = {
        "active_section": "payments",
        "requests": PaymentRequest.objects.select_related(
            "organization",
            "article",
            "counterparty",
            "contract",
            "currency",
            "approver",
            "author",
        ),
        "organizations": Organization.objects.all(),
        "articles": CashFlowArticle.objects.all(),
        "counterparties": Counterparty.objects.all(),
        "contracts": Contract.objects.filter(kind=ContractKind.SOLE_SUPPLIER).select_related("counterparty", "currency"),
        "currencies": Currency.objects.all(),
        "approvers": User.objects.filter(profile__role=UserRole.MANAGER),
        "status": PaymentRequestStatus,
        "request_kind": PaymentRequestKind,
        "can_manage_payment_requests": _can_manage_payment_requests(request.user),
        "can_approve_payment_requests": _can_approve_plans(request.user),
        "limit_control_mode": limit_settings.control_mode,
        "limit_control_mode_label": (
            "Блокировка" if limit_settings.control_mode == PaymentLimitControlMode.BLOCK else "Предупреждение"
        ),
    }
    return render(request, "core/payment_requests.html", context)


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def payment_facts(request):
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "adjust":
                if not _can_edit_payment_facts(request.user):
                    raise ValueError("Корректировать факт может только экономист или администратор")
                fact = get_object_or_404(PaymentFact, pk=request.POST.get("fact_id"))
                adjust_payment_fact(
                    payment_fact=fact,
                    author=request.user,
                    new_amount=_parse_decimal(request.POST.get("new_amount")),
                    reason=request.POST.get("reason", ""),
                    new_comment=request.POST.get("new_comment", ""),
                )
                messages.success(request, "Корректировка факта сохранена")
        except (InvalidOperation, ValueError) as exc:
            messages.error(request, str(exc))
        return redirect("payment_facts")

    current_year = timezone.localdate().year
    selected_year = _parse_optional_int(request.GET.get("year")) or current_year
    selected_month = _parse_optional_int(request.GET.get("month"))
    selected_accounting_kind = (request.GET.get("accounting_kind") or "").strip()
    if selected_accounting_kind not in ("", "bu", "nu"):
        selected_accounting_kind = ""
    selected_article_id = _parse_optional_int(request.GET.get("article_id"))
    selected_counterparty_id = _parse_optional_int(request.GET.get("counterparty_id"))

    facts_query = PaymentFact.objects.select_related(
        "organization", "article", "counterparty", "contract", "currency"
    ).filter(date__year=selected_year)
    if selected_month:
        facts_query = facts_query.filter(date__month=selected_month)
    if selected_accounting_kind:
        facts_query = facts_query.filter(accounting_kind=selected_accounting_kind)
    if selected_article_id:
        facts_query = facts_query.filter(article_id=selected_article_id)
    if selected_counterparty_id:
        facts_query = facts_query.filter(counterparty_id=selected_counterparty_id)

    facts = list(facts_query.order_by("-date", "-id"))
    fact_ids = [fact.id for fact in facts]
    adjustments_stats = {
        row["payment_fact_id"]: row
        for row in PaymentFactAdjustment.objects.filter(payment_fact_id__in=fact_ids)
        .values("payment_fact_id")
        .annotate(versions=Count("id"), latest_version=Max("version"), latest_at=Max("created_at"))
    }

    rows = []
    for fact in facts:
        stat = adjustments_stats.get(fact.id, {})
        rows.append(
            {
                "fact": fact,
                "versions": stat.get("versions", 0),
                "latest_version": stat.get("latest_version", 0),
                "latest_at": stat.get("latest_at"),
            }
        )

    context = {
        "active_section": "payments",
        "rows": rows,
        "current_year": current_year,
        "years": list(range(current_year - 1, current_year + 3)),
        "months": list(range(1, 13)),
        "articles": CashFlowArticle.objects.order_by("code"),
        "counterparties": Counterparty.objects.order_by("name"),
        "selected_year": selected_year,
        "selected_month": selected_month,
        "selected_accounting_kind": selected_accounting_kind,
        "selected_article_id": selected_article_id,
        "selected_counterparty_id": selected_counterparty_id,
        "can_edit_payment_facts": _can_edit_payment_facts(request.user),
    }
    return render(request, "core/payment_facts.html", context)


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def plan_fact_report(request):
    current_year = timezone.localdate().year
    selected_year = _parse_optional_int(request.GET.get("year")) or current_year
    selected_status = (request.GET.get("request_status") or "").strip()
    if selected_status not in PaymentRequestStatus.values:
        selected_status = ""

    filters = PlanFactFilters(
        year=selected_year,
        article_id=_parse_optional_int(request.GET.get("article_id")),
        counterparty_id=_parse_optional_int(request.GET.get("counterparty_id")),
        request_status=selected_status,
    )
    rows, summary = build_plan_fact_report(filters)

    if request.GET.get("export") == "excel":
        response = HttpResponse(plan_fact_to_csv(rows), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="plan_fact_{selected_year}.csv"'
        return response

    context = {
        "active_section": "reports",
        "rows": rows,
        "summary": summary,
        "years": list(range(current_year - 1, current_year + 3)),
        "articles": CashFlowArticle.objects.order_by("code"),
        "counterparties": Counterparty.objects.order_by("name"),
        "request_status_choices": [("", "Все статусы")] + list(PaymentRequestStatus.choices),
        "selected_year": selected_year,
        "selected_article_id": filters.article_id,
        "selected_counterparty_id": filters.counterparty_id,
        "selected_request_status": selected_status,
    }
    return render(request, "core/plan_fact_report.html", context)


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


def _parse_optional_int(raw_value):
    if not raw_value:
        return None
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


def _parse_optional_date(raw_value: str | None) -> date | None:
    if not raw_value:
        return None
    try:
        return date.fromisoformat(raw_value)
    except ValueError:
        return None


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


def _can_manage_payment_requests(user) -> bool:
    if user.is_superuser:
        return True
    profile = getattr(user, "profile", None)
    return bool(profile and profile.role in [UserRole.ADMINISTRATOR, UserRole.ECONOMIST])


def _can_edit_payment_facts(user) -> bool:
    if user.is_superuser:
        return True
    profile = getattr(user, "profile", None)
    return bool(profile and profile.role in [UserRole.ADMINISTRATOR, UserRole.ECONOMIST])
