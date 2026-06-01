"""Кейсы финансирования: список, карточка-сводка, CRUD."""

from django.contrib import messages
from django.db import IntegrityError
from django.shortcuts import get_object_or_404, redirect, render

from decimal import Decimal, InvalidOperation

from django.utils import timezone

from ..access import role_required
from ..models import (
    AccountingKind,
    AuditAction,
    AuditLog,
    CashFlowArticle,
    Contract,
    ContractKind,
    FundingCase,
    FundingCaseStatus,
    Organization,
    PaymentDirection,
    PaymentFact,
    UserRole,
)
from ..services.funding_cases import build_case_overview
from ._shared import parse_decimal, parse_optional_int, working_organization


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def funding_cases_index(request):
    org = working_organization(request)

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "create":
                case = _create_case(request, org)
                messages.success(request, f"Кейс «{case.name}» создан")
                return redirect("funding_case_detail", case_id=case.id)
            if action == "close":
                case = get_object_or_404(FundingCase, pk=request.POST.get("case_id"))
                case.status = FundingCaseStatus.CLOSED
                from django.utils import timezone
                case.closed_at = timezone.localdate()
                case.save(update_fields=["status", "closed_at", "updated_at"])
                AuditLog.objects.create(
                    user=request.user,
                    action=AuditAction.UPDATE,
                    path="/funding/cases/",
                    object_type="FundingCase",
                    object_id=str(case.pk),
                    message=f"Кейс {case.code} закрыт",
                )
                messages.success(request, f"Кейс «{case.name}» закрыт")
        except (IntegrityError, ValueError) as exc:
            messages.error(request, _format_err(exc))
        return redirect("funding_cases_index")

    cases_qs = FundingCase.objects.select_related("organization", "owner").order_by("-opened_at")
    if org:
        cases_qs = cases_qs.filter(organization=org)
    status_filter = (request.GET.get("status") or "").strip()
    if status_filter in FundingCaseStatus.values:
        cases_qs = cases_qs.filter(status=status_filter)

    cases_with_balance = []
    from decimal import Decimal as _D
    hero_stats = {
        "total": 0,
        "active": 0,
        "closed": 0,
        "deficit": 0,
        "total_inflow": _D("0"),
        "total_outflow": _D("0"),
    }
    for case in cases_qs[:50]:
        overview = build_case_overview(case)
        # Прогресс по обороту: какая доля от max(inflow+expected, outflow+expected) уже прошла фактом
        total_flow = overview["total_inflow"] + overview["total_outflow"]
        future_flow = overview["expected_inflow_remaining"] + overview["expected_outflow_remaining"]
        progress = 100
        if (total_flow + future_flow) > 0:
            progress = int(round(float(total_flow) / float(total_flow + future_flow) * 100))
        cases_with_balance.append({
            "case": case,
            "overview": overview,
            "progress": min(100, max(0, progress)),
            "is_deficit": overview["projected_balance"] < 0,
        })
        hero_stats["total"] += 1
        if case.status == FundingCaseStatus.ACTIVE:
            hero_stats["active"] += 1
        if case.status == FundingCaseStatus.CLOSED:
            hero_stats["closed"] += 1
        if overview["projected_balance"] < 0:
            hero_stats["deficit"] += 1
        hero_stats["total_inflow"] += overview["total_inflow"]
        hero_stats["total_outflow"] += overview["total_outflow"]

    return render(
        request,
        "core/funding_cases_index.html",
        {
            "active_section": "funding",
            "cases_with_balance": cases_with_balance,
            "hero_stats": hero_stats,
            "organizations": Organization.objects.order_by("name"),
            "working_organization": org,
            "status_choices": [("", "Все статусы")] + list(FundingCaseStatus.choices),
            "selected_status": status_filter,
        },
    )


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def funding_case_detail(request, case_id: int):
    case = get_object_or_404(FundingCase, pk=case_id)

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "attach_contract":
                contract = get_object_or_404(Contract, pk=request.POST.get("contract_id"))
                contract.funding_case = case
                contract.save(update_fields=["funding_case"])
                AuditLog.objects.create(
                    user=request.user,
                    action=AuditAction.UPDATE,
                    path=f"/funding/cases/{case.pk}/",
                    object_type="Contract",
                    object_id=str(contract.pk),
                    message=f"Договор {contract.number} привязан к кейсу {case.code}",
                )
                messages.success(request, f"Договор {contract.number} привязан к кейсу")
            elif action == "detach_contract":
                contract = get_object_or_404(Contract, pk=request.POST.get("contract_id"))
                contract.funding_case = None
                contract.save(update_fields=["funding_case"])
                messages.success(request, f"Договор {contract.number} отвязан от кейса")
            elif action == "update":
                case.name = (request.POST.get("name") or case.name).strip()
                case.description = (request.POST.get("description") or "").strip()
                status = request.POST.get("status")
                if status in FundingCaseStatus.values:
                    case.status = status
                case.save(update_fields=["name", "description", "status", "updated_at"])
                messages.success(request, "Кейс обновлён")
            elif action == "record_loan_repayment":
                fact = _record_loan_repayment(request, case)
                messages.success(
                    request,
                    f"Записан возврат займа {fact.loan_repayment_contract.number}: "
                    f"{fact.amount} ₽ (из них процентов {fact.loan_interest_portion})",
                )
        except (IntegrityError, ValueError) as exc:
            messages.error(request, _format_err(exc))
        return redirect("funding_case_detail", case_id=case.id)

    overview = build_case_overview(case)
    # Доступные для привязки договоры — без кейса или из этой же организации
    attachable_contracts = Contract.objects.filter(
        funding_case__isnull=True,
        counterparty__isnull=False,
    ).select_related("counterparty").order_by("-date")[:30]

    # Статьи ДДС для quick-action «Записать возврат займа» (outflow)
    repayment_articles = CashFlowArticle.objects.filter(
        direction="outflow",
        is_group=False,
    ).order_by("code")

    return render(
        request,
        "core/funding_case_detail.html",
        {
            "active_section": "funding",
            "case": case,
            "overview": overview,
            "attachable_contracts": attachable_contracts,
            "repayment_articles": repayment_articles,
            "today": timezone.localdate(),
            "kind_labels": dict(ContractKind.choices),
            "status_choices": FundingCaseStatus.choices,
        },
    )


def _record_loan_repayment(request, case: FundingCase) -> PaymentFact:
    """Quick-action: создаёт PaymentFact-возврат конкретного займа кейса с
    проставленными loan_repayment_contract + loan_interest_portion. Освобождает
    финансиста от навигации в отдельный wizard."""
    loan = get_object_or_404(
        Contract,
        pk=parse_optional_int(request.POST.get("loan_contract_id")),
        kind=ContractKind.LOAN_RECEIVED,
        funding_case=case,
    )
    article = get_object_or_404(
        CashFlowArticle, pk=parse_optional_int(request.POST.get("article_id"))
    )

    fact_date = request.POST.get("date") or timezone.localdate().isoformat()
    try:
        amount = parse_decimal(request.POST.get("amount"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Некорректная сумма: {exc}") from exc
    if not amount or amount <= 0:
        raise ValueError("Сумма возврата должна быть больше нуля")
    interest_raw = (request.POST.get("interest_portion") or "0").strip().replace(",", ".")
    try:
        interest = Decimal(interest_raw)
    except InvalidOperation as exc:
        raise ValueError(f"Некорректная сумма процентов: {exc}") from exc
    if interest < 0 or interest > amount:
        raise ValueError("Сумма процентов должна быть в диапазоне 0..сумма возврата")

    fact = PaymentFact.objects.create(
        external_id=f"case-{case.pk}-loan-{loan.pk}-{int(timezone.now().timestamp())}",
        date=fact_date,
        organization=case.organization,
        article=article,
        counterparty=loan.counterparty,
        contract=loan,
        loan_repayment_contract=loan,
        loan_interest_portion=interest,
        amount=amount,
        currency=loan.currency,
        accounting_kind=AccountingKind.BU,
        direction=PaymentDirection.OUTFLOW,
        account="51",
        comment=f"Возврат займа · кейс {case.code}",
    )
    AuditLog.objects.create(
        user=request.user,
        action=AuditAction.CREATE,
        path=f"/funding/cases/{case.pk}/",
        object_type="PaymentFact",
        object_id=str(fact.pk),
        message=f"Записан возврат займа {loan.number} в кейсе {case.code}: {amount} ₽ (% {interest})",
    )
    return fact


# Helpers

def _create_case(request, org) -> FundingCase:
    code = (request.POST.get("code") or "").strip().upper()
    name = (request.POST.get("name") or "").strip()
    if not code:
        raise ValueError("Укажите код кейса (например, FUND-2026-001)")
    if not name:
        raise ValueError("Укажите название кейса")
    organization_id = parse_optional_int(request.POST.get("organization_id"))
    organization = (
        get_object_or_404(Organization, pk=organization_id)
        if organization_id
        else org
    )
    if organization is None:
        raise ValueError("Укажите рабочую компанию или выберите организацию")
    case = FundingCase.objects.create(
        code=code,
        name=name,
        organization=organization,
        description=(request.POST.get("description") or "").strip(),
        owner=request.user,
        status=FundingCaseStatus.ACTIVE,
    )
    AuditLog.objects.create(
        user=request.user,
        action=AuditAction.CREATE,
        path="/funding/cases/",
        object_type="FundingCase",
        object_id=str(case.pk),
        message=f"Создан кейс финансирования {case.code} ({case.name})",
    )
    return case


def _format_err(exc) -> str:
    if isinstance(exc, IntegrityError):
        return "Кейс с таким кодом уже существует"
    return str(exc)
