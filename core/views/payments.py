"""Payments domain: requests, edit, journal, facts, fact adjustments, downloads."""

from decimal import InvalidOperation
from mimetypes import guess_type
from pathlib import Path

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Max
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from ..access import role_required
from ..models import (
    AdditionalAgreement,
    AuditAction,
    AuditLog,
    CashFlowArticle,
    Contract,
    ContractKind,
    Counterparty,
    Currency,
    Organization,
    PaymentFact,
    PaymentFactAdjustment,
    PaymentLimitControlMode,
    PaymentRequest,
    PaymentRequestControlSettings,
    PaymentRequestKind,
    PaymentRequestStatus,
    UserRole,
)
from ..services.payment_fact_adjustments import adjust_payment_fact
from ..services.payment_requests import (
    approve_payment_request,
    create_payment_request,
    reject_payment_request,
    submit_payment_request,
    transfer_payment_request_to_do,
    update_payment_request,
)
from ._permissions import (
    can_approve_payment_requests,
    can_create_payment_facts,
    can_create_payment_requests,
    can_delete_payment_facts,
    can_delete_payment_requests,
    can_edit_payment_facts,
    can_manage_payment_requests,
    can_view_payment_facts,
)
from ._shared import (
    has_model_permission,
    is_administrator,
    parse_decimal,
    parse_optional_date,
    parse_optional_int,
    working_organization,
)


User = get_user_model()


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def payment_requests(request):
    org = working_organization(request)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "create":
                if not can_create_payment_requests(request.user):
                    raise ValueError("Создавать заявки может только экономист или администратор")
                _create_payment_request_from_request(request)
                messages.success(request, "Заявка создана")
            elif action == "submit":
                if not can_manage_payment_requests(request.user):
                    raise ValueError("Отправлять заявки может только экономист или администратор")
                submit_payment_request(
                    get_object_or_404(PaymentRequest, pk=request.POST.get("request_id")),
                    request.user,
                )
                messages.success(request, "Заявка отправлена на согласование")
            elif action == "approve":
                if not can_approve_payment_requests(request.user):
                    raise ValueError("Согласовывать заявки может только назначенный руководитель")
                approve_payment_request(
                    get_object_or_404(PaymentRequest, pk=request.POST.get("request_id")),
                    request.user,
                )
                messages.success(request, "Заявка согласована")
            elif action == "reject":
                if not can_approve_payment_requests(request.user):
                    raise ValueError("Отклонять заявки может только назначенный руководитель")
                reject_payment_request(
                    get_object_or_404(PaymentRequest, pk=request.POST.get("request_id")),
                    request.user,
                    request.POST.get("approver_comment", ""),
                )
                messages.success(request, "Заявка отклонена")
            elif action == "transfer":
                if not can_manage_payment_requests(request.user):
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
    return render(
        request,
        "core/payment_requests.html",
        {
            "active_section": "payments",
            "requests": PaymentRequest.objects.select_related(
                "organization", "article", "counterparty", "contract",
                "additional_agreement", "currency", "approver", "author",
            ).filter(organization=org),
            "organizations": Organization.objects.all(),
            "working_organization": org,
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
                {"id": agreement.id, "contract_id": agreement.contract_id}
                for agreement in agreements
            ],
            "currencies": Currency.objects.all(),
            "approvers": User.objects.filter(profile__role=UserRole.MANAGER),
            "status": PaymentRequestStatus,
            "request_kind": PaymentRequestKind,
            "can_create_payment_requests": can_create_payment_requests(request.user),
            "can_manage_payment_requests": can_manage_payment_requests(request.user),
            "can_approve_payment_requests": can_approve_payment_requests(request.user),
            "payments_acl": {
                "view": has_model_permission(request.user, PaymentRequest, "view"),
                "create": can_create_payment_requests(request.user),
                "edit": can_manage_payment_requests(request.user),
                "delete": can_delete_payment_requests(request.user),
            },
            "limit_control_mode": limit_settings.control_mode,
            "limit_control_mode_label": (
                "Блокировка" if limit_settings.control_mode == PaymentLimitControlMode.BLOCK else "Предупреждение"
            ),
        },
    )


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def payment_request_wizard(request):
    can_create_request = can_create_payment_requests(request.user) and can_manage_payment_requests(request.user)
    org = working_organization(request)
    submitted: dict = {}
    if request.method == "POST":
        try:
            if not can_create_request:
                raise ValueError("Заявку на оплату может создать экономист или администратор")
            payment_request = _create_payment_request_from_request(request)
            submit_payment_request(payment_request, request.user)
            messages.success(request, f"Заявка {payment_request.number} отправлена на согласование")
            return redirect("payment_requests")
        except (InvalidOperation, ValueError) as exc:
            messages.error(request, str(exc))
            submitted = request.POST

    return render(
        request,
        "core/payment_request_wizard.html",
        {
            "active_section": "payments",
            "request_kind": PaymentRequestKind,
            "can_create_request": can_create_request,
            "working_organization": org,
            "submitted": submitted,
            **_payment_request_reference_context(),
        },
    )


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def payment_request_edit_wizard(request, request_id: int):
    payment_request = get_object_or_404(PaymentRequest, pk=request_id)
    can_edit_request = can_manage_payment_requests(request.user) and payment_request.status in [
        PaymentRequestStatus.DRAFT,
        PaymentRequestStatus.REJECTED,
    ]
    submitted: dict = {}
    if request.method == "POST":
        try:
            if not can_edit_request:
                raise ValueError("Редактировать заявку может экономист или администратор")
            _update_payment_request_from_request(payment_request, request)
            messages.success(request, f"Заявка {payment_request.number} сохранена")
            return redirect("payment_requests")
        except (InvalidOperation, ValueError) as exc:
            messages.error(request, str(exc))
            submitted = request.POST

    return render(
        request,
        "core/payment_request_edit_wizard.html",
        {
            "active_section": "payments",
            "payment_request": payment_request,
            "request_kind": PaymentRequestKind,
            "can_edit_request": can_edit_request,
            "working_organization": working_organization(request),
            "submitted": submitted,
            **_payment_request_reference_context(),
        },
    )


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def payment_facts(request):
    org = working_organization(request)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "adjust":
                if not can_edit_payment_facts(request.user):
                    raise ValueError("Корректировать факт может только экономист или администратор")
                fact = get_object_or_404(PaymentFact, pk=request.POST.get("fact_id"))
                adjust_payment_fact(
                    payment_fact=fact,
                    author=request.user,
                    new_amount=parse_decimal(request.POST.get("new_amount")),
                    reason=request.POST.get("reason", ""),
                    new_comment=request.POST.get("new_comment", ""),
                )
                messages.success(request, "Корректировка факта сохранена")
        except (InvalidOperation, ValueError) as exc:
            messages.error(request, str(exc))
        return redirect("payment_facts")

    current_year = timezone.localdate().year
    selected_year = parse_optional_int(request.GET.get("year")) or current_year
    selected_month = parse_optional_int(request.GET.get("month"))
    selected_accounting_kind = (request.GET.get("accounting_kind") or "").strip()
    if selected_accounting_kind not in ("", "bu", "nu"):
        selected_accounting_kind = ""
    selected_article_id = parse_optional_int(request.GET.get("article_id"))
    selected_counterparty_id = parse_optional_int(request.GET.get("counterparty_id"))

    facts_query = PaymentFact.objects.select_related(
        "organization", "article", "counterparty", "contract", "currency"
    ).filter(organization=org, date__year=selected_year)
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

    return render(
        request,
        "core/payment_facts.html",
        {
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
            "working_organization": org,
            "can_view_payment_facts": can_view_payment_facts(request.user),
            "can_edit_payment_facts": can_edit_payment_facts(request.user),
            "payment_facts_acl": {
                "view": can_view_payment_facts(request.user),
                "create": can_create_payment_facts(request.user),
                "edit": can_edit_payment_facts(request.user),
                "delete": can_delete_payment_facts(request.user),
            },
        },
    )


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def payment_fact_adjustment_wizard(request):
    submitted: dict = {}
    if request.method == "POST":
        try:
            if not can_edit_payment_facts(request.user):
                raise ValueError("Корректировать факт может только экономист или администратор")
            fact = get_object_or_404(PaymentFact, pk=request.POST.get("fact_id"))
            adjustment = adjust_payment_fact(
                payment_fact=fact,
                author=request.user,
                new_amount=parse_decimal(request.POST.get("new_amount")),
                reason=request.POST.get("reason", ""),
                new_comment=request.POST.get("new_comment", ""),
            )
            messages.success(request, f"Корректировка факта v{adjustment.version} сохранена")
            return redirect("payment_facts")
        except (InvalidOperation, ValueError) as exc:
            messages.error(request, str(exc))
            submitted = request.POST

    facts = PaymentFact.objects.select_related(
        "organization", "article", "counterparty", "contract", "currency",
    ).order_by("-date", "-id")[:200]
    return render(
        request,
        "core/payment_fact_adjustment_wizard.html",
        {
            "active_section": "payments",
            "facts": facts,
            "selected_fact_id": parse_optional_int(request.GET.get("fact_id")),
            "can_edit_payment_facts": can_edit_payment_facts(request.user),
            "submitted": submitted,
        },
    )


@login_required
def payment_request_justification_download(request, request_id: int):
    """Authenticated, access-checked download of the justification file.

    Files are stored under MEDIA_ROOT with UUID names, but MEDIA_URL must NOT
    be exposed in production. This view enforces who can see a given file:
    author, approver, or administrator.
    """
    payment_request = get_object_or_404(PaymentRequest, pk=request_id)
    if not payment_request.justification_file:
        raise Http404("К заявке не приложен файл обоснования")

    user = request.user
    allowed = (
        is_administrator(user)
        or payment_request.author_id == user.id
        or payment_request.approver_id == user.id
    )
    if not allowed:
        raise PermissionDenied

    AuditLog.objects.create(
        user=user,
        action=AuditAction.VIEW,
        path=request.path,
        object_type="PaymentRequest.justification_file",
        object_id=str(payment_request.id),
        message=f"Скачан файл обоснования заявки {payment_request.number}",
    )

    file_field = payment_request.justification_file
    suffix = Path(file_field.name).suffix.lower()
    content_type = guess_type(file_field.name)[0] or "application/octet-stream"
    download_name = f"justification-{payment_request.number}{suffix}"
    response = FileResponse(file_field.open("rb"), content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{download_name}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


# --- POST extraction helpers ----------------------------------------------

def _payment_request_reference_context():
    contracts = list(
        Contract.objects.filter(kind=ContractKind.SOLE_SUPPLIER).select_related("counterparty", "currency")
    )
    agreements = list(
        AdditionalAgreement.objects.select_related("contract", "currency", "contract__counterparty").order_by(
            "date", "number",
        )
    )
    return {
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
            {"id": agreement.id, "contract_id": agreement.contract_id}
            for agreement in agreements
        ],
        "currencies": Currency.objects.all(),
        "approvers": User.objects.filter(profile__role=UserRole.MANAGER),
    }


def _create_payment_request_from_request(request):
    contract = None
    contract_id = request.POST.get("contract_id")
    if contract_id:
        contract = get_object_or_404(Contract, pk=contract_id)

    additional_agreement = None
    additional_agreement_id = request.POST.get("additional_agreement_id")
    if additional_agreement_id:
        additional_agreement = get_object_or_404(AdditionalAgreement, pk=additional_agreement_id)

    counterparty = contract.counterparty if contract else get_object_or_404(
        Counterparty, pk=request.POST.get("counterparty_id"),
    )
    currency = contract.currency if contract else get_object_or_404(
        Currency, pk=request.POST.get("currency_id"),
    )
    amount_raw = (request.POST.get("amount") or "").strip()
    amount = parse_decimal(amount_raw) if amount_raw else (contract.amount if contract else None)
    manual_exchange_rate_raw = (request.POST.get("manual_exchange_rate") or "").strip()
    manual_exchange_rate = (
        parse_decimal(manual_exchange_rate_raw)
        if manual_exchange_rate_raw
        else (contract.manual_exchange_rate if contract else None)
    )
    return create_payment_request(
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
        invoice_date=parse_optional_date(request.POST.get("invoice_date")),
        payment_purpose=request.POST.get("payment_purpose", ""),
        comment=request.POST.get("comment", ""),
        justification_text=request.POST.get("justification_text", ""),
        justification_file=request.FILES.get("justification_file"),
    )


def _update_payment_request_from_request(payment_request, request):
    contract = None
    contract_id = request.POST.get("contract_id")
    if contract_id:
        contract = get_object_or_404(Contract, pk=contract_id)

    additional_agreement = None
    additional_agreement_id = request.POST.get("additional_agreement_id")
    if additional_agreement_id:
        additional_agreement = get_object_or_404(AdditionalAgreement, pk=additional_agreement_id)

    counterparty = contract.counterparty if contract else get_object_or_404(
        Counterparty, pk=request.POST.get("counterparty_id"),
    )
    currency = contract.currency if contract else get_object_or_404(
        Currency, pk=request.POST.get("currency_id"),
    )
    amount_raw = (request.POST.get("amount") or "").strip()
    amount = parse_decimal(amount_raw) if amount_raw else (contract.amount if contract else None)
    manual_exchange_rate_raw = (request.POST.get("manual_exchange_rate") or "").strip()
    manual_exchange_rate = (
        parse_decimal(manual_exchange_rate_raw)
        if manual_exchange_rate_raw
        else (contract.manual_exchange_rate if contract else None)
    )
    return update_payment_request(
        request=payment_request,
        user=request.user,
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
        invoice_date=parse_optional_date(request.POST.get("invoice_date")),
        payment_purpose=request.POST.get("payment_purpose", ""),
        comment=request.POST.get("comment", ""),
        justification_text=request.POST.get("justification_text", ""),
        justification_file=request.FILES.get("justification_file"),
    )
