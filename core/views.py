from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from mimetypes import guess_type
import html
import re
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.db import connection
from django.db.models import Count, Max, Q, Sum
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone
from django.utils.safestring import mark_safe

from .models import (
    AdditionalAgreement,
    AuditAction,
    AuditLog,
    BudgetLimitAdjustment,
    BudgetLimitPlan,
    BudgetPlanStatus,
    CashFlowArticle,
    Contract,
    ContractKind,
    Counterparty,
    Currency,
    Department,
    IntegrationRequest,
    IntegrationRequestStatus,
    Nomenclature,
    Organization,
    PaymentFact,
    PaymentFactAdjustment,
    PaymentLimitControlMode,
    PaymentRequest,
    PaymentRequestControlSettings,
    PaymentRequestKind,
    PaymentRequestStatus,
    ReportTemplate,
    ReportTemplateType,
    SyncRun,
    UiThemeMode,
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
from .services.integration_requests import create_integration_request, update_integration_request_status
from .services.manager_dashboard import build_manager_dashboard
from .services.payment_requests import (
    approve_payment_request,
    create_payment_request,
    reject_payment_request,
    submit_payment_request,
    transfer_payment_request_to_do,
)
from .services.payment_fact_adjustments import adjust_payment_fact
from .services.plan_fact_report import PlanFactFilters, build_plan_fact_report, to_csv as plan_fact_to_csv


User = get_user_model()
BASE_DIR = Path(__file__).resolve().parent.parent
USER_GUIDE_DIR = BASE_DIR / "docs" / "user-guide"


class RoleAwareLoginView(LoginView):
    template_name = "registration/login.html"

    def get_success_url(self):
        profile = getattr(self.request.user, "profile", None)
        if profile and profile.role == UserRole.ACCOUNTANT:
            return reverse("external_accounting")
        return reverse("workspace")


@login_required
def set_theme_mode(request):
    mode = request.POST.get("theme_mode")
    if mode not in UiThemeMode.values:
        mode = UiThemeMode.LIGHT

    profile = getattr(request.user, "profile", None)
    if profile:
        profile.theme_mode = mode
        profile.save(update_fields=["theme_mode", "updated_at"])
    request.session["ui_theme_mode"] = mode

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or reverse("workspace")
    if not url_has_allowed_host_and_scheme(next_url, {request.get_host()}):
        next_url = reverse("workspace")
    return redirect(next_url)


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def integration_requests(request):
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "create":
                create_integration_request(
                    requested_by=request.user,
                    integration_name=request.POST.get("integration_name", ""),
                    target_system=request.POST.get("target_system", ""),
                    description=request.POST.get("description", ""),
                )
                messages.success(request, "Заявка на интеграцию создана")
            elif action == "set_status":
                if not _is_administrator(request.user):
                    raise ValueError("Изменять статус заявки может только администратор")
                update_integration_request_status(
                    request=get_object_or_404(IntegrationRequest, pk=request.POST.get("request_id")),
                    admin_user=request.user,
                    status=request.POST.get("status", ""),
                    admin_comment=request.POST.get("admin_comment", ""),
                )
                messages.success(request, "Статус заявки обновлен")
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect("integration_requests")

    selected_status = (request.GET.get("status") or "").strip()
    if selected_status not in IntegrationRequestStatus.values:
        selected_status = ""

    requests_query = IntegrationRequest.objects.select_related("requested_by", "assigned_admin")
    if selected_status:
        requests_query = requests_query.filter(status=selected_status)
    requests_list = list(requests_query)

    context = {
        "active_section": "settings",
        "requests": requests_list,
        "status_choices": [("", "Все статусы")] + list(IntegrationRequestStatus.choices),
        "selected_status": selected_status,
        "can_manage_integration_requests": _is_administrator(request.user),
    }
    return render(request, "core/integration_requests.html", context)


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
            "document": "Дашборд руководителя",
            "state": "Готово",
            "check": "Остатки + превышения + статусы",
        },
        {
            "name": "Настройки",
            "document": "Заявки на интеграцию",
            "state": "В работе",
            "check": "Запрос администратору",
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
                if not _can_create_plans(request.user):
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
                if not _has_model_permission(request.user, BudgetLimitAdjustment, "add"):
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
                if not _has_model_permission(request.user, BudgetLimitAdjustment, "change"):
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
        "can_create_plans": _can_create_plans(request.user),
        "can_approve_plans": _can_approve_plans(request.user),
        "plans_acl": {
            "view": _has_model_permission(request.user, BudgetLimitPlan, "view"),
            "create": _can_create_plans(request.user),
            "edit": _can_edit_plans(request.user),
            "delete": _can_delete_plans(request.user),
        },
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
                if not _can_create_payment_requests(request.user):
                    raise ValueError("Создавать заявки может только экономист или администратор")
                contract = None
                contract_id = request.POST.get("contract_id")
                if contract_id:
                    contract = get_object_or_404(Contract, pk=contract_id)
                additional_agreement = None
                additional_agreement_id = request.POST.get("additional_agreement_id")
                if additional_agreement_id:
                    additional_agreement = get_object_or_404(AdditionalAgreement, pk=additional_agreement_id)
                counterparty = contract.counterparty if contract else get_object_or_404(
                    Counterparty,
                    pk=request.POST.get("counterparty_id"),
                )
                currency = contract.currency if contract else get_object_or_404(
                    Currency,
                    pk=request.POST.get("currency_id"),
                )
                amount_raw = (request.POST.get("amount") or "").strip()
                amount = _parse_decimal(amount_raw) if amount_raw else (contract.amount if contract else None)
                manual_exchange_rate_raw = (request.POST.get("manual_exchange_rate") or "").strip()
                manual_exchange_rate = (
                    _parse_decimal(manual_exchange_rate_raw)
                    if manual_exchange_rate_raw
                    else (contract.manual_exchange_rate if contract else None)
                )
                create_payment_request(
                    author=request.user,
                    request_kind=request.POST.get("request_kind"),
                    organization=get_object_or_404(Organization, pk=request.POST.get("organization_id")),
                    article=get_object_or_404(CashFlowArticle, pk=request.POST.get("article_id")),
                    counterparty=counterparty,
                    contract=contract,
                    additional_agreement=additional_agreement,
                    currency=currency,
                    amount=amount,
                    manual_exchange_rate=manual_exchange_rate,
                    approver=get_object_or_404(User, pk=request.POST.get("approver_id")),
                    invoice_number=request.POST.get("invoice_number", ""),
                    invoice_date=_parse_optional_date(request.POST.get("invoice_date")),
                    payment_purpose=request.POST.get("payment_purpose", ""),
                    comment=request.POST.get("comment", ""),
                    justification_text=request.POST.get("justification_text", ""),
                    justification_file=request.FILES.get("justification_file"),
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
                if not _can_approve_payment_requests(request.user):
                    raise ValueError("Согласовывать заявки может только назначенный руководитель")
                approve_payment_request(
                    get_object_or_404(PaymentRequest, pk=request.POST.get("request_id")),
                    request.user,
                )
                messages.success(request, "Заявка согласована")
            elif action == "reject":
                if not _can_approve_payment_requests(request.user):
                    raise ValueError("Отклонять заявки может только назначенный руководитель")
                reject_payment_request(
                    get_object_or_404(PaymentRequest, pk=request.POST.get("request_id")),
                    request.user,
                    request.POST.get("approver_comment", ""),
                )
                messages.success(request, "Заявка отклонена")
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

    contracts = list(
        Contract.objects.filter(kind=ContractKind.SOLE_SUPPLIER).select_related("counterparty", "currency")
    )
    agreements = list(
        AdditionalAgreement.objects.select_related("contract", "currency", "contract__counterparty").order_by("date", "number")
    )
    limit_settings = PaymentRequestControlSettings.active_or_default()
    context = {
        "active_section": "payments",
        "requests": PaymentRequest.objects.select_related(
            "organization",
            "article",
            "counterparty",
            "contract",
            "additional_agreement",
            "currency",
            "approver",
            "author",
        ),
        "organizations": Organization.objects.all(),
        "articles": CashFlowArticle.objects.all(),
        "counterparties": Counterparty.objects.all(),
        "contracts": contracts,
        "agreements": agreements,
        "contracts_autofill": [
            {
                "id": contract.id,
                "counterparty_id": contract.counterparty_id,
                "currency_id": contract.currency_id,
                "amount": str(contract.amount),
                "manual_exchange_rate": str(contract.manual_exchange_rate),
            }
            for contract in contracts
        ],
        "agreements_autofill": [
            {
                "id": agreement.id,
                "contract_id": agreement.contract_id,
            }
            for agreement in agreements
        ],
        "currencies": Currency.objects.all(),
        "approvers": User.objects.filter(profile__role=UserRole.MANAGER),
        "status": PaymentRequestStatus,
        "request_kind": PaymentRequestKind,
        "can_create_payment_requests": _can_create_payment_requests(request.user),
        "can_manage_payment_requests": _can_manage_payment_requests(request.user),
        "can_approve_payment_requests": _can_approve_payment_requests(request.user),
        "payments_acl": {
            "view": _has_model_permission(request.user, PaymentRequest, "view"),
            "create": _can_create_payment_requests(request.user),
            "edit": _can_manage_payment_requests(request.user),
            "delete": _can_delete_payment_requests(request.user),
        },
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
        "can_view_payment_facts": _can_view_payment_facts(request.user),
        "can_edit_payment_facts": _can_edit_payment_facts(request.user),
        "payment_facts_acl": {
            "view": _can_view_payment_facts(request.user),
            "create": _can_create_payment_facts(request.user),
            "edit": _can_edit_payment_facts(request.user),
            "delete": _can_delete_payment_facts(request.user),
        },
    }
    return render(request, "core/payment_facts.html", context)


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def _legacy_plan_fact_report(request):
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


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def plan_fact_report(request):
    current_year = timezone.localdate().year
    templates = list(
        ReportTemplate.objects.filter(owner=request.user, report_type=ReportTemplateType.PLAN_FACT).order_by("name")
    )
    templates_by_id = {template.id: template for template in templates}
    default_template = next((template for template in templates if template.is_default), None)

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "apply_template":
                template = _resolve_report_template(templates_by_id, request.POST.get("template_id"))
                query = _plan_fact_payload_to_query(template.filters, template_id=template.id)
                return redirect(f"{reverse('plan_fact_report')}?{query}")

            if action in {"save_template", "delete_template"} and not _can_manage_report_templates(request.user):
                raise ValueError("Сохранять и удалять шаблоны может только пользователь с правом создания шаблонов")

            if action == "save_template":
                template_name = (request.POST.get("template_name") or "").strip()
                if not template_name:
                    raise ValueError("Укажите название шаблона")
                payload = _collect_plan_fact_filter_payload(request.POST, default_year=current_year)
                template, created = ReportTemplate.objects.update_or_create(
                    owner=request.user,
                    report_type=ReportTemplateType.PLAN_FACT,
                    name=template_name,
                    defaults={
                        "filters": payload,
                        "is_default": request.POST.get("is_default") == "1",
                    },
                )
                messages.success(request, "Шаблон отчета создан" if created else "Шаблон отчета обновлен")
                query = _plan_fact_payload_to_query(payload, template_id=template.id)
                return redirect(f"{reverse('plan_fact_report')}?{query}")
            if action == "delete_template":
                template = _resolve_report_template(templates_by_id, request.POST.get("template_id"))
                template.delete()
                messages.success(request, "Шаблон отчета удален")
                return redirect("plan_fact_report")
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("plan_fact_report")

    selected_template_id = _parse_optional_int(request.GET.get("template_id"))
    selected_template = templates_by_id.get(selected_template_id) if selected_template_id else None

    base_filters = {}
    if selected_template:
        base_filters = selected_template.filters or {}
    elif not request.GET and default_template:
        base_filters = default_template.filters or {}
        selected_template = default_template
        selected_template_id = default_template.id

    selected_year = _read_filter_int(request.GET, base_filters, "year") or current_year
    selected_status = _read_filter_text(
        request.GET,
        base_filters,
        "request_status",
        allowed_values=PaymentRequestStatus.values,
    )
    selected_article_id = _read_filter_int(request.GET, base_filters, "article_id")
    selected_counterparty_id = _read_filter_int(request.GET, base_filters, "counterparty_id")
    selected_organization_id = _read_filter_int(request.GET, base_filters, "organization_id")
    selected_customer_contract_id = _read_filter_int(request.GET, base_filters, "customer_contract_id")
    selected_supplier_contract_id = _read_filter_int(request.GET, base_filters, "supplier_contract_id")

    filters = PlanFactFilters(
        year=selected_year,
        article_id=selected_article_id,
        counterparty_id=selected_counterparty_id,
        organization_id=selected_organization_id,
        customer_contract_id=selected_customer_contract_id,
        supplier_contract_id=selected_supplier_contract_id,
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
        "organizations": Organization.objects.order_by("name"),
        "counterparties": Counterparty.objects.order_by("name"),
        "customer_contracts": Contract.objects.filter(kind=ContractKind.CUSTOMER).order_by("number"),
        "supplier_contracts": Contract.objects.filter(kind=ContractKind.SOLE_SUPPLIER).order_by("number"),
        "request_status_choices": [("", "Все статусы")] + list(PaymentRequestStatus.choices),
        "selected_year": selected_year,
        "selected_article_id": selected_article_id,
        "selected_counterparty_id": selected_counterparty_id,
        "selected_organization_id": selected_organization_id,
        "selected_customer_contract_id": selected_customer_contract_id,
        "selected_supplier_contract_id": selected_supplier_contract_id,
        "selected_request_status": selected_status,
        "report_templates": templates,
        "selected_template_id": selected_template_id,
        "can_manage_report_templates": _can_manage_report_templates(request.user),
        "active_filters_payload": {
            "year": selected_year,
            "article_id": selected_article_id,
            "organization_id": selected_organization_id,
            "counterparty_id": selected_counterparty_id,
            "customer_contract_id": selected_customer_contract_id,
            "supplier_contract_id": selected_supplier_contract_id,
            "request_status": selected_status,
        },
    }
    return render(request, "core/plan_fact_report.html", context)


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def manager_dashboard(request):
    current_year = timezone.localdate().year
    selected_year = _parse_optional_int(request.GET.get("year")) or current_year
    payload = build_manager_dashboard(year=selected_year)
    context = {
        "active_section": "reports",
        "years": list(range(current_year - 1, current_year + 3)),
        "selected_year": selected_year,
        **payload,
    }
    return render(request, "core/manager_dashboard.html", context)


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


@login_required
def instruction(request):
    guide_path = USER_GUIDE_DIR / "instruction.md"
    try:
        markdown_text = guide_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Http404("Руководство не найдено") from exc

    context = {
        "active_section": "instruction",
        "guide_html": mark_safe(_render_user_guide_markdown(markdown_text)),
    }
    return render(request, "core/instruction.html", context)


@login_required
def instruction_asset(request, asset_path: str):
    asset_root = USER_GUIDE_DIR.resolve()
    requested_path = Path(asset_path)
    if requested_path.is_absolute() or ".." in requested_path.parts:
        raise Http404("Некорректный путь")

    resolved_path = (USER_GUIDE_DIR / requested_path).resolve()
    if asset_root not in resolved_path.parents:
        raise Http404("Файл не найден")
    if not resolved_path.is_file():
        raise Http404("Файл не найден")

    allowed_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    if resolved_path.suffix.lower() not in allowed_suffixes:
        raise Http404("Тип файла не поддерживается")

    content_type = guess_type(str(resolved_path))[0] or "application/octet-stream"
    return FileResponse(resolved_path.open("rb"), content_type=content_type)


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


def _resolve_report_template(templates_by_id: dict[int, ReportTemplate], raw_template_id) -> ReportTemplate:
    template_id = _parse_optional_int(raw_template_id)
    if not template_id:
        raise ValueError("Выберите шаблон отчета")
    template = templates_by_id.get(template_id)
    if template is None:
        raise ValueError("Шаблон отчета не найден")
    return template


def _read_filter_int(query_params, base_filters: dict, key: str) -> int | None:
    if key in query_params:
        return _parse_optional_int(query_params.get(key))
    return _parse_optional_int(base_filters.get(key))


def _read_filter_text(query_params, base_filters: dict, key: str, allowed_values) -> str:
    if key in query_params:
        raw_value = (query_params.get(key) or "").strip()
    else:
        fallback = base_filters.get(key)
        raw_value = str(fallback).strip() if fallback is not None else ""
    if raw_value not in allowed_values:
        return ""
    return raw_value


def _collect_plan_fact_filter_payload(data, *, default_year: int) -> dict:
    year = _parse_optional_int(data.get("year")) or default_year
    request_status = (data.get("request_status") or "").strip()
    if request_status not in PaymentRequestStatus.values:
        request_status = ""
    return {
        "year": year,
        "article_id": _parse_optional_int(data.get("article_id")),
        "counterparty_id": _parse_optional_int(data.get("counterparty_id")),
        "organization_id": _parse_optional_int(data.get("organization_id")),
        "customer_contract_id": _parse_optional_int(data.get("customer_contract_id")),
        "supplier_contract_id": _parse_optional_int(data.get("supplier_contract_id")),
        "request_status": request_status,
    }


def _plan_fact_payload_to_query(payload: dict, template_id: int | None = None) -> str:
    query = {}
    for key, value in payload.items():
        if value in (None, ""):
            continue
        query[key] = str(value)
    if template_id:
        query["template_id"] = str(template_id)
    return urlencode(query)


def _can_edit_plans(user) -> bool:
    return _has_model_permission(user, BudgetLimitPlan, "change")


def _can_create_plans(user) -> bool:
    return _has_model_permission(user, BudgetLimitPlan, "add")


def _can_delete_plans(user) -> bool:
    return _has_model_permission(user, BudgetLimitPlan, "delete")


def _can_approve_plans(user) -> bool:
    if user.is_superuser:
        return True
    profile = getattr(user, "profile", None)
    return bool(profile and profile.role in [UserRole.ADMINISTRATOR, UserRole.MANAGER])


def _can_approve_payment_requests(user) -> bool:
    return _can_approve_plans(user) and _has_model_permission(user, PaymentRequest, "view")


def _can_manage_payment_requests(user) -> bool:
    return _has_model_permission(user, PaymentRequest, "change")


def _can_create_payment_requests(user) -> bool:
    return _has_model_permission(user, PaymentRequest, "add")


def _can_delete_payment_requests(user) -> bool:
    return _has_model_permission(user, PaymentRequest, "delete")


def _can_edit_payment_facts(user) -> bool:
    return _has_model_permission(user, PaymentFact, "change")


def _can_view_payment_facts(user) -> bool:
    return _has_model_permission(user, PaymentFact, "view")


def _can_create_payment_facts(user) -> bool:
    return _has_model_permission(user, PaymentFact, "add")


def _can_delete_payment_facts(user) -> bool:
    return _has_model_permission(user, PaymentFact, "delete")


def _can_manage_report_templates(user) -> bool:
    return _has_model_permission(user, ReportTemplate, "add")


def _is_administrator(user) -> bool:
    if user.is_superuser:
        return True
    profile = getattr(user, "profile", None)
    return bool(profile and profile.role == UserRole.ADMINISTRATOR)


def _has_model_permission(user, model, action: str) -> bool:
    if not user.is_authenticated or not user.is_active:
        return False
    if user.is_superuser:
        return True
    return user.has_perm(f"{model._meta.app_label}.{action}_{model._meta.model_name}")


_IMAGE_PATTERN = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_ORDERED_LIST_PATTERN = re.compile(r"\d+\.\s+(.*)")
_UNORDERED_LIST_PATTERN = re.compile(r"-\s+(.*)")


def _render_user_guide_markdown(markdown_text: str) -> str:
    lines = markdown_text.splitlines()
    chunks = ['<article class="guide-content">']
    index = 0

    while index < len(lines):
        line = lines[index].rstrip()
        stripped = line.strip()

        if not stripped:
            index += 1
            continue

        if stripped.startswith("### "):
            chunks.append(f"<h3>{_render_inline_markdown(stripped[4:])}</h3>")
            index += 1
            continue
        if stripped.startswith("## "):
            chunks.append(f"<h2>{_render_inline_markdown(stripped[3:])}</h2>")
            index += 1
            continue
        if stripped.startswith("# "):
            chunks.append(f"<h1>{_render_inline_markdown(stripped[2:])}</h1>")
            index += 1
            continue
        if stripped == "---":
            chunks.append("<hr>")
            index += 1
            continue

        image_match = _IMAGE_PATTERN.fullmatch(stripped)
        if image_match:
            alt_text = _render_inline_markdown(image_match.group(1))
            image_src = image_match.group(2).strip()
            image_url = reverse("instruction_asset", kwargs={"asset_path": image_src})
            chunks.append(
                (
                    '<figure class="guide-figure">'
                    f'<img src="{image_url}" alt="{html.escape(image_match.group(1))}" loading="lazy">'
                    f"<figcaption>{alt_text}</figcaption>"
                    "</figure>"
                )
            )
            index += 1
            continue

        if _is_table_line(stripped):
            table_lines = []
            while index < len(lines) and _is_table_line(lines[index].strip()):
                table_lines.append(lines[index].strip())
                index += 1
            chunks.append(_render_markdown_table(table_lines))
            continue

        ordered_match = _ORDERED_LIST_PATTERN.match(stripped)
        if ordered_match:
            list_items = []
            while index < len(lines):
                current_line = lines[index].strip()
                match = _ORDERED_LIST_PATTERN.match(current_line)
                if not match:
                    break
                list_items.append(f"<li>{_render_inline_markdown(match.group(1))}</li>")
                index += 1
            chunks.append("<ol>" + "".join(list_items) + "</ol>")
            continue

        unordered_match = _UNORDERED_LIST_PATTERN.match(stripped)
        if unordered_match:
            list_items = []
            while index < len(lines):
                current_line = lines[index].strip()
                match = _UNORDERED_LIST_PATTERN.match(current_line)
                if not match:
                    break
                list_items.append(f"<li>{_render_inline_markdown(match.group(1))}</li>")
                index += 1
            chunks.append("<ul>" + "".join(list_items) + "</ul>")
            continue

        paragraph_lines = [stripped]
        index += 1
        while index < len(lines):
            current_line = lines[index].strip()
            if not current_line:
                break
            if _starts_block(current_line):
                break
            paragraph_lines.append(current_line)
            index += 1
        paragraph = " ".join(paragraph_lines)
        chunks.append(f"<p>{_render_inline_markdown(paragraph)}</p>")

    chunks.append("</article>")
    return "".join(chunks)


def _render_markdown_table(table_lines: list[str]) -> str:
    if not table_lines:
        return ""

    rows = [[cell.strip() for cell in line.strip("|").split("|")] for line in table_lines]
    if not rows:
        return ""

    separator_index = None
    if len(rows) > 1 and all(set(cell.replace(":", "").replace("-", "").strip()) == set() for cell in rows[1]):
        separator_index = 1

    headers = rows[0]
    data_rows = rows[2:] if separator_index is not None else rows[1:]

    head_html = "".join(f"<th>{_render_inline_markdown(header)}</th>" for header in headers)
    body_html = "".join(
        "<tr>" + "".join(f"<td>{_render_inline_markdown(cell)}</td>" for cell in row) + "</tr>"
        for row in data_rows
    )
    return (
        '<div class="data-table-wrap guide-table-wrap"><table class="data-table guide-table">'
        f"<thead><tr>{head_html}</tr></thead><tbody>{body_html}</tbody></table></div>"
    )


def _render_inline_markdown(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    return escaped


def _is_table_line(line: str) -> bool:
    return line.startswith("|") and line.endswith("|")


def _starts_block(line: str) -> bool:
    return (
        line.startswith("#")
        or line == "---"
        or bool(_IMAGE_PATTERN.fullmatch(line))
        or bool(_ORDERED_LIST_PATTERN.match(line))
        or bool(_UNORDERED_LIST_PATTERN.match(line))
        or _is_table_line(line)
    )
