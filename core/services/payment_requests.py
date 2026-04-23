from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core.models import (
    AuditAction,
    AuditLog,
    BudgetLimitPlan,
    BudgetPlanStatus,
    Contract,
    ContractKind,
    PaymentDirection,
    PaymentFact,
    PaymentLimitControlMode,
    PaymentRequest,
    PaymentRequestControlSettings,
    PaymentRequestKind,
    PaymentRequestStatus,
)


MONEY_QUANT = Decimal("0.01")
RUB_CODE = "RUB"
WORKFLOW_PATH = "/payments/requests/"
RESERVED_REQUEST_STATUSES = [
    PaymentRequestStatus.PENDING_APPROVAL,
    PaymentRequestStatus.APPROVED,
    PaymentRequestStatus.TRANSFERRED,
]


@transaction.atomic
def create_payment_request(
    *,
    author,
    request_kind: str,
    organization,
    article,
    counterparty,
    contract: Contract | None,
    currency,
    amount: Decimal,
    manual_exchange_rate: Decimal | None,
    approver,
    invoice_number: str = "",
    comment: str = "",
) -> PaymentRequest:
    _validate_request_payload(
        request_kind=request_kind,
        counterparty=counterparty,
        contract=contract,
        invoice_number=invoice_number,
        amount=amount,
        manual_exchange_rate=manual_exchange_rate,
    )
    amount_rub = amount_to_rub(currency.code, amount, manual_exchange_rate)
    before_limit, after_limit, is_exceeded = calculate_limit_delta(article=article, request_amount_rub=amount_rub)
    control_settings = PaymentRequestControlSettings.active_or_default()
    _enforce_limit_control_mode(control_settings.control_mode, is_exceeded)

    request = PaymentRequest.objects.create(
        number=next_payment_request_number(),
        request_kind=request_kind,
        request_date=timezone.localdate(),
        organization=organization,
        article=article,
        counterparty=counterparty,
        contract=contract,
        invoice_number=invoice_number.strip(),
        currency=currency,
        amount=amount.quantize(MONEY_QUANT),
        manual_exchange_rate=_normalized_rate(currency.code, manual_exchange_rate),
        amount_rub=amount_rub,
        limit_remaining_before_rub=before_limit,
        limit_remaining_after_rub=after_limit,
        limit_exceeded=is_exceeded,
        approver=approver,
        author=author,
        comment=comment,
    )
    AuditLog.objects.create(
        user=author,
        action=AuditAction.CREATE,
        path=WORKFLOW_PATH,
        object_type="PaymentRequest",
        object_id=str(request.pk),
        message=f"Создана заявка на оплату {request.number}",
    )
    return request


@transaction.atomic
def submit_payment_request(request: PaymentRequest, user) -> PaymentRequest:
    if request.status != PaymentRequestStatus.DRAFT:
        raise ValueError("На согласование можно отправить только черновик")

    before_limit, after_limit, is_exceeded = calculate_limit_delta(
        article=request.article,
        request_amount_rub=request.amount_rub,
        exclude_request_id=request.id,
    )
    control_settings = PaymentRequestControlSettings.active_or_default()
    _enforce_limit_control_mode(control_settings.control_mode, is_exceeded)

    request.status = PaymentRequestStatus.PENDING_APPROVAL
    request.limit_remaining_before_rub = before_limit
    request.limit_remaining_after_rub = after_limit
    request.limit_exceeded = is_exceeded
    request.save(
        update_fields=[
            "status",
            "limit_remaining_before_rub",
            "limit_remaining_after_rub",
            "limit_exceeded",
            "updated_at",
        ]
    )
    AuditLog.objects.create(
        user=user,
        action=AuditAction.UPDATE,
        path=WORKFLOW_PATH,
        object_type="PaymentRequest",
        object_id=str(request.pk),
        message=f"Заявка {request.number} отправлена на согласование",
    )
    return request


@transaction.atomic
def approve_payment_request(request: PaymentRequest, user) -> PaymentRequest:
    if request.status != PaymentRequestStatus.PENDING_APPROVAL:
        raise ValueError("Согласовать можно только заявку в статусе 'На согласовании'")
    if request.approver_id != user.id and not user.is_superuser:
        raise ValueError("Заявку может согласовать только назначенный руководитель")

    before_limit, after_limit, is_exceeded = calculate_limit_delta(
        article=request.article,
        request_amount_rub=request.amount_rub,
        exclude_request_id=request.id,
    )
    control_settings = PaymentRequestControlSettings.active_or_default()
    _enforce_limit_control_mode(control_settings.control_mode, is_exceeded)

    request.status = PaymentRequestStatus.APPROVED
    request.approved_at = timezone.now()
    request.limit_remaining_before_rub = before_limit
    request.limit_remaining_after_rub = after_limit
    request.limit_exceeded = is_exceeded
    request.save(
        update_fields=[
            "status",
            "approved_at",
            "limit_remaining_before_rub",
            "limit_remaining_after_rub",
            "limit_exceeded",
            "updated_at",
        ]
    )
    AuditLog.objects.create(
        user=user,
        action=AuditAction.APPROVE,
        path=WORKFLOW_PATH,
        object_type="PaymentRequest",
        object_id=str(request.pk),
        message=f"Заявка {request.number} согласована",
    )
    return request


@transaction.atomic
def transfer_payment_request_to_do(request: PaymentRequest, user) -> PaymentRequest:
    if request.status != PaymentRequestStatus.APPROVED:
        raise ValueError("Передавать в 1С:ДО можно только согласованную заявку")

    request.status = PaymentRequestStatus.TRANSFERRED
    request.transferred_at = timezone.now()
    request.do_external_id = next_do_external_id()
    request.save(update_fields=["status", "transferred_at", "do_external_id", "updated_at"])
    AuditLog.objects.create(
        user=user,
        action=AuditAction.TRANSFER,
        path=WORKFLOW_PATH,
        object_type="PaymentRequest",
        object_id=str(request.pk),
        message=f"Заявка {request.number} передана в 1С:ДО как {request.do_external_id}",
    )
    return request


def calculate_limit_delta(*, article, request_amount_rub: Decimal, exclude_request_id: int | None = None) -> tuple[Decimal, Decimal, bool]:
    approved_limit = (
        BudgetLimitPlan.objects.filter(article=article, status=BudgetPlanStatus.APPROVED)
        .aggregate(total=Sum("annual_amount"))
        .get("total")
        or Decimal("0")
    )
    paid_amount = spent_amount_rub_by_article(article_id=article.id)
    reserved_by_requests = reserved_amount_rub_by_article(article_id=article.id, exclude_request_id=exclude_request_id)

    before_limit = quant_money(approved_limit - paid_amount - reserved_by_requests)
    after_limit = quant_money(before_limit - request_amount_rub)
    return before_limit, after_limit, after_limit < 0


def spent_amount_rub_by_article(*, article_id: int) -> Decimal:
    facts = PaymentFact.objects.filter(
        article_id=article_id,
        accounting_kind="bu",
        direction=PaymentDirection.OUTFLOW,
    ).select_related("currency", "contract")
    total = Decimal("0")
    for fact in facts:
        total += amount_to_rub(
            currency_code=fact.currency.code,
            amount=fact.amount,
            manual_exchange_rate=(fact.contract.manual_exchange_rate if fact.contract else None),
        )
    return quant_money(total)


def reserved_amount_rub_by_article(*, article_id: int, exclude_request_id: int | None = None) -> Decimal:
    query = PaymentRequest.objects.filter(article_id=article_id, status__in=RESERVED_REQUEST_STATUSES)
    if exclude_request_id:
        query = query.exclude(pk=exclude_request_id)
    return query.aggregate(total=Sum("amount_rub")).get("total") or Decimal("0")


def amount_to_rub(currency_code: str, amount: Decimal, manual_exchange_rate: Decimal | None) -> Decimal:
    if amount <= 0:
        raise ValueError("Сумма заявки должна быть больше нуля")
    if currency_code == RUB_CODE:
        return quant_money(amount)
    rate = manual_exchange_rate or Decimal("0")
    if rate <= 0:
        raise ValueError("Для валютной заявки укажите курс к RUB больше нуля")
    return quant_money(amount * rate)


def next_payment_request_number() -> str:
    return f"REQ-{timezone.localdate():%Y}-{PaymentRequest.objects.count() + 1:06d}"


def next_do_external_id() -> str:
    current = PaymentRequest.objects.exclude(do_external_id="").count() + 1
    return f"DO-{timezone.localdate():%Y}-{current:06d}"


def _validate_request_payload(
    *,
    request_kind: str,
    counterparty,
    contract: Contract | None,
    invoice_number: str,
    amount: Decimal,
    manual_exchange_rate: Decimal | None,
) -> None:
    if request_kind not in PaymentRequestKind.values:
        raise ValueError("Неизвестный тип заявки")
    if amount is None:
        raise ValueError("Укажите сумму заявки")
    if amount <= 0:
        raise ValueError("Сумма заявки должна быть больше нуля")
    if request_kind == PaymentRequestKind.BY_CONTRACT and contract is None:
        raise ValueError("Для заявки по договору выберите договор")
    if request_kind == PaymentRequestKind.BY_INVOICE and not invoice_number.strip():
        raise ValueError("Для заявки по счету укажите номер счета")
    if request_kind == PaymentRequestKind.WITHOUT_CONTRACT and contract is not None:
        raise ValueError("Для заявки без договора поле договора должно быть пустым")
    if contract is not None:
        if contract.kind != ContractKind.SOLE_SUPPLIER:
            raise ValueError("В заявке на оплату можно использовать только договор поставщика")
        if contract.counterparty_id != counterparty.id:
            raise ValueError("Контрагент заявки должен совпадать с контрагентом договора")
    if manual_exchange_rate is not None and manual_exchange_rate < 0:
        raise ValueError("Курс к RUB не может быть отрицательным")


def _normalized_rate(currency_code: str, manual_exchange_rate: Decimal | None) -> Decimal:
    if currency_code == RUB_CODE:
        return Decimal("1.0000")
    rate = manual_exchange_rate or Decimal("0")
    if rate <= 0:
        raise ValueError("Для валютной заявки укажите курс к RUB больше нуля")
    return rate.quantize(Decimal("0.0001"))


def _enforce_limit_control_mode(control_mode: str, is_exceeded: bool) -> None:
    if is_exceeded and control_mode == PaymentLimitControlMode.BLOCK:
        raise ValueError("Лимит превышен. В текущем режиме создание заявки заблокировано")


def quant_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANT)
