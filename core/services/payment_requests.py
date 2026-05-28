from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core.models import (
    AdditionalAgreement,
    AuditAction,
    AuditLog,
    BudgetLimitPlan,
    BudgetPlanStatus,
    Contract,
    ContractKind,
    NotificationKind,
    PaymentDirection,
    PaymentFact,
    PaymentLimitControlMode,
    PaymentRequest,
    PaymentRequestControlSettings,
    PaymentRequestKind,
    PaymentRequestStatus,
)
from core.services.document_numbers import next_document_number
from core.services.notifications import notify
from core.validators import validate_justification_file


MONEY_QUANT = Decimal("0.01")
RUB_CODE = "RUB"
WORKFLOW_PATH = "/payments/requests/"
RESERVED_REQUEST_STATUSES = [
    PaymentRequestStatus.PENDING_APPROVAL,
    PaymentRequestStatus.PENDING_FINAL_APPROVAL,
    PaymentRequestStatus.APPROVED,
    PaymentRequestStatus.TRANSFERRED,
]
PENDING_APPROVAL_STATUSES = (
    PaymentRequestStatus.PENDING_APPROVAL,
    PaymentRequestStatus.PENDING_FINAL_APPROVAL,
)


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
    additional_agreement: AdditionalAgreement | None = None,
    invoice_number: str = "",
    invoice_date=None,
    payment_purpose: str = "",
    comment: str = "",
    justification_text: str = "",
    justification_file=None,
) -> PaymentRequest:
    _validate_request_payload(
        request_kind=request_kind,
        counterparty=counterparty,
        contract=contract,
        additional_agreement=additional_agreement,
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        payment_purpose=payment_purpose,
        justification_text=justification_text,
        amount=amount,
        manual_exchange_rate=manual_exchange_rate,
    )
    try:
        validate_justification_file(justification_file)
    except ValidationError as exc:
        raise ValueError("; ".join(exc.messages)) from exc

    amount_rub = amount_to_rub(currency.code, amount, manual_exchange_rate)
    before_limit, after_limit, is_exceeded = calculate_limit_delta(
        article=article,
        organization=organization,
        request_amount_rub=amount_rub,
    )
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
        additional_agreement=additional_agreement,
        invoice_number=invoice_number.strip(),
        invoice_date=invoice_date,
        payment_purpose=payment_purpose.strip(),
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
        justification_text=justification_text.strip(),
        justification_file=justification_file,
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
def update_payment_request(
    *,
    request: PaymentRequest,
    user,
    request_kind: str,
    organization,
    article,
    counterparty,
    contract: Contract | None,
    currency,
    amount: Decimal,
    manual_exchange_rate: Decimal | None,
    approver,
    additional_agreement: AdditionalAgreement | None = None,
    invoice_number: str = "",
    invoice_date=None,
    payment_purpose: str = "",
    comment: str = "",
    justification_text: str = "",
    justification_file=None,
) -> PaymentRequest:
    if request.status not in [PaymentRequestStatus.DRAFT, PaymentRequestStatus.REJECTED]:
        raise ValueError("Редактировать можно только черновик или отклоненную заявку")

    _validate_request_payload(
        request_kind=request_kind,
        counterparty=counterparty,
        contract=contract,
        additional_agreement=additional_agreement,
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        payment_purpose=payment_purpose,
        justification_text=justification_text,
        amount=amount,
        manual_exchange_rate=manual_exchange_rate,
    )
    if justification_file:
        try:
            validate_justification_file(justification_file)
        except ValidationError as exc:
            raise ValueError("; ".join(exc.messages)) from exc

    amount_rub = amount_to_rub(currency.code, amount, manual_exchange_rate)
    before_limit, after_limit, is_exceeded = calculate_limit_delta(
        article=article,
        organization=organization,
        request_amount_rub=amount_rub,
        exclude_request_id=request.id,
    )
    control_settings = PaymentRequestControlSettings.active_or_default()
    _enforce_limit_control_mode(control_settings.control_mode, is_exceeded)

    request.request_kind = request_kind
    request.organization = organization
    request.article = article
    request.counterparty = counterparty
    request.contract = contract
    request.additional_agreement = additional_agreement
    request.invoice_number = invoice_number.strip()
    request.invoice_date = invoice_date
    request.payment_purpose = payment_purpose.strip()
    request.currency = currency
    request.amount = amount.quantize(MONEY_QUANT)
    request.manual_exchange_rate = _normalized_rate(currency.code, manual_exchange_rate)
    request.amount_rub = amount_rub
    request.limit_remaining_before_rub = before_limit
    request.limit_remaining_after_rub = after_limit
    request.limit_exceeded = is_exceeded
    request.approver = approver
    request.comment = comment
    request.justification_text = justification_text.strip()
    if justification_file:
        request.justification_file = justification_file
    request.status = PaymentRequestStatus.DRAFT
    request.approver_comment = ""
    request.rejected_at = None
    request.save()
    AuditLog.objects.create(
        user=user,
        action=AuditAction.UPDATE,
        path=WORKFLOW_PATH,
        object_type="PaymentRequest",
        object_id=str(request.pk),
        message=f"Заявка на оплату {request.number} отредактирована",
    )
    return request


@transaction.atomic
def submit_payment_request(request: PaymentRequest, user) -> PaymentRequest:
    if request.status != PaymentRequestStatus.DRAFT:
        raise ValueError("На согласование можно отправить только черновик")

    before_limit, after_limit, is_exceeded = calculate_limit_delta(
        article=request.article,
        organization=request.organization,
        request_amount_rub=request.amount_rub,
        exclude_request_id=request.id,
    )
    control_settings = PaymentRequestControlSettings.active_or_default()
    _enforce_limit_control_mode(control_settings.control_mode, is_exceeded)

    request.status = PaymentRequestStatus.PENDING_APPROVAL
    request.submitted_at = timezone.now()
    request.limit_remaining_before_rub = before_limit
    request.limit_remaining_after_rub = after_limit
    request.limit_exceeded = is_exceeded
    request.save(
        update_fields=[
            "status",
            "submitted_at",
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
    # Уведомить согласующего о новой заявке
    if request.approver_id:
        notify(
            recipient=request.approver,
            kind=NotificationKind.PAYMENT_SUBMITTED,
            title=f"Новая заявка на согласование · {request.number}",
            text=f"{request.counterparty.name} · {request.amount} {request.currency.code}"
                 + (" · превышение лимита" if is_exceeded else ""),
            link="/payments/requests/",
            related=request,
        )
    # При превышении лимита в warning-режиме (block-режим вообще не пропустил
    # бы заявку) — сигнал и автору, и согласующему о выходе за бюджет.
    if is_exceeded:
        overrun_text = (
            f"{request.article.code} · {request.counterparty.name} · "
            f"{request.amount} {request.currency.code} · "
            f"остаток после: {after_limit}"
        )
        notify(
            recipient=request.author,
            kind=NotificationKind.LIMIT_OVERRUN,
            title=f"Превышение лимита по заявке {request.number}",
            text=overrun_text,
            link="/reports/manager/",
            related=request,
        )
        if request.approver_id and request.approver_id != request.author_id:
            notify(
                recipient=request.approver,
                kind=NotificationKind.LIMIT_OVERRUN,
                title=f"Превышение лимита по заявке {request.number}",
                text=overrun_text,
                link="/reports/manager/",
                related=request,
            )
    return request


@transaction.atomic
def approve_payment_request(request: PaymentRequest, user) -> PaymentRequest:
    if request.status not in PENDING_APPROVAL_STATUSES:
        raise ValueError("Согласовать можно только заявку в статусе 'На согласовании'")
    if request.approver_id != user.id and not user.is_superuser:
        raise ValueError("Заявку может согласовать только назначенный руководитель")

    before_limit, after_limit, is_exceeded = calculate_limit_delta(
        article=request.article,
        organization=request.organization,
        request_amount_rub=request.amount_rub,
        exclude_request_id=request.id,
    )
    control_settings = PaymentRequestControlSettings.active_or_default()
    _enforce_limit_control_mode(control_settings.control_mode, is_exceeded)

    # Escalation: first approval of a request whose amount crosses the
    # organization threshold transitions to PENDING_FINAL_APPROVAL with the
    # configured secondary approver instead of finalizing.
    if request.status == PaymentRequestStatus.PENDING_APPROVAL:
        org = request.organization
        threshold = org.escalation_threshold_rub
        secondary = org.secondary_approver
        if (
            threshold is not None
            and secondary is not None
            and request.amount_rub >= threshold
            and secondary.id != user.id
        ):
            request.status = PaymentRequestStatus.PENDING_FINAL_APPROVAL
            request.approver = secondary
            request.limit_remaining_before_rub = before_limit
            request.limit_remaining_after_rub = after_limit
            request.limit_exceeded = is_exceeded
            request.save(
                update_fields=[
                    "status",
                    "approver",
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
                message=f"Заявка {request.number}: первичное согласование, передано на финальное согласование",
            )
            notify(
                recipient=secondary,
                kind=NotificationKind.PAYMENT_SUBMITTED,
                title=f"Финальное согласование: заявка {request.number}",
                text=f"{request.counterparty.name} · {request.amount} {request.currency.code}",
                link="/payments/requests/",
                related=request,
            )
            return request

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
    # Уведомить автора
    notify(
        recipient=request.author,
        kind=NotificationKind.PAYMENT_APPROVED,
        title=f"Заявка {request.number} согласована",
        text=f"{request.counterparty.name} · {request.amount} {request.currency.code}",
        link="/payments/requests/",
        related=request,
    )
    return request


@transaction.atomic
def reject_payment_request(request: PaymentRequest, user, approver_comment: str) -> PaymentRequest:
    if request.status not in PENDING_APPROVAL_STATUSES:
        raise ValueError("Отклонить можно только заявку в статусе 'На согласовании'")
    if request.approver_id != user.id and not user.is_superuser:
        raise ValueError("Заявку может отклонить только назначенный руководитель")
    if not approver_comment.strip():
        raise ValueError("При отклонении укажите комментарий согласующего")

    request.status = PaymentRequestStatus.REJECTED
    request.approver_comment = approver_comment.strip()
    request.rejected_at = timezone.now()
    request.save(update_fields=["status", "approver_comment", "rejected_at", "updated_at"])
    AuditLog.objects.create(
        user=user,
        action=AuditAction.UPDATE,
        path=WORKFLOW_PATH,
        object_type="PaymentRequest",
        object_id=str(request.pk),
        message=f"Заявка {request.number} отклонена",
    )
    # Уведомить автора с причиной отклонения
    notify(
        recipient=request.author,
        kind=NotificationKind.PAYMENT_REJECTED,
        title=f"Заявка {request.number} отклонена",
        text=approver_comment.strip()[:400],
        link=f"/payments/requests/{request.pk}/edit-wizard/",
        related=request,
    )
    return request


@transaction.atomic
def cancel_payment_request(request: PaymentRequest, user, reason: str = "") -> PaymentRequest:
    """Author-driven cancellation for DRAFT or PENDING_APPROVAL requests.

    Cancelling a PENDING request frees its reserved limit, so other pending
    requests on the same article need to be recomputed. The approver is
    notified that the request was withdrawn.
    """
    if request.status not in (PaymentRequestStatus.DRAFT, *PENDING_APPROVAL_STATUSES):
        raise ValueError("Отменить можно только черновик или заявку на согласовании")
    if request.author_id != user.id and not user.is_superuser:
        raise ValueError("Заявку может отменить только её автор")

    was_pending = request.status in PENDING_APPROVAL_STATUSES
    request.status = PaymentRequestStatus.CANCELLED
    request.cancelled_at = timezone.now()
    request.cancellation_reason = (reason or "").strip()[:255]
    request.save(update_fields=["status", "cancelled_at", "cancellation_reason", "updated_at"])
    AuditLog.objects.create(
        user=user,
        action=AuditAction.UPDATE,
        path=WORKFLOW_PATH,
        object_type="PaymentRequest",
        object_id=str(request.pk),
        message=f"Заявка {request.number} отменена автором",
    )
    # Уведомить согласующего, если заявка успела дойти до него
    if was_pending and request.approver_id and request.approver_id != user.id:
        notify(
            recipient=request.approver,
            kind=NotificationKind.PAYMENT_REJECTED,  # Reuse — для approver это «снято с очереди»
            title=f"Заявка {request.number} отозвана автором",
            text=request.cancellation_reason or f"{request.counterparty.name} · {request.amount} {request.currency.code}",
            link="/payments/requests/",
            related=request,
        )
    # Отмена pending-заявки освобождает резерв → пересчитать соседей по статье
    if was_pending:
        recalculate_pending_requests_for_limit(
            article=request.article,
            organization=request.organization,
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
    # Уведомить автора, что заявка ушла во внешний контур
    notify(
        recipient=request.author,
        kind=NotificationKind.PAYMENT_TRANSFERRED,
        title=f"Заявка {request.number} передана в 1С:ДО",
        text=f"Внешний ID: {request.do_external_id}",
        link="/payments/requests/",
        related=request,
    )
    return request


def recalculate_pending_requests_for_limit(*, article, organization) -> int:
    """Refresh limit_remaining/exceeded on PENDING_APPROVAL requests after a
    limit change. Returns the number of requests updated.

    When the exceeded flag flips, notify the author and the approver so they
    see why the document changed shape: a budget action by someone else just
    made their request over/under limit.
    """
    pending = (
        PaymentRequest.objects.filter(
            article=article,
            organization=organization,
            status__in=PENDING_APPROVAL_STATUSES,
        )
        .select_related("author", "approver", "currency", "counterparty")
    )
    touched = 0
    for pr in pending:
        before, after, exceeded = calculate_limit_delta(
            article=article,
            organization=organization,
            request_amount_rub=pr.amount_rub,
            exclude_request_id=pr.id,
        )
        if (
            pr.limit_remaining_before_rub == before
            and pr.limit_remaining_after_rub == after
            and pr.limit_exceeded == exceeded
        ):
            continue
        flipped_to_exceeded = exceeded and not pr.limit_exceeded
        flipped_to_within = (not exceeded) and pr.limit_exceeded
        pr.limit_remaining_before_rub = before
        pr.limit_remaining_after_rub = after
        pr.limit_exceeded = exceeded
        pr.save(update_fields=[
            "limit_remaining_before_rub",
            "limit_remaining_after_rub",
            "limit_exceeded",
            "updated_at",
        ])
        touched += 1
        if flipped_to_exceeded or flipped_to_within:
            text = (
                f"{pr.counterparty.name} · {pr.amount} {pr.currency.code} · "
                f"остаток после: {after}"
            )
            kind = NotificationKind.LIMIT_OVERRUN if flipped_to_exceeded else NotificationKind.LIMIT_APPROVED
            title = (
                f"Заявка {pr.number}: превышение лимита после пересчёта"
                if flipped_to_exceeded
                else f"Заявка {pr.number}: вошла в лимит после пересчёта"
            )
            # Уведомляем обе стороны заявки: смена статуса превышения — это
            # side-effect действия по другому документу (корректировка / новый
            # лимит), который автор и согласующий могли не заметить.
            for recipient in {pr.author, pr.approver}:
                if recipient is None:
                    continue
                notify(
                    recipient=recipient,
                    kind=kind,
                    title=title,
                    text=text,
                    link="/payments/requests/",
                    related=pr,
                )
    return touched


def calculate_limit_delta(
    *, article, organization, request_amount_rub: Decimal, exclude_request_id: int | None = None
) -> tuple[Decimal, Decimal, bool]:
    approved_limit = (
        BudgetLimitPlan.objects.filter(article=article, organization=organization, status=BudgetPlanStatus.APPROVED)
        .aggregate(total=Sum("annual_amount"))
        .get("total")
        or Decimal("0")
    )
    paid_amount = spent_amount_rub_by_article(article_id=article.id, organization_id=organization.id)
    reserved_by_requests = reserved_amount_rub_by_article(
        article_id=article.id,
        organization_id=organization.id,
        exclude_request_id=exclude_request_id,
    )

    before_limit = quant_money(approved_limit - paid_amount - reserved_by_requests)
    after_limit = quant_money(before_limit - request_amount_rub)
    return before_limit, after_limit, after_limit < 0


def spent_amount_rub_by_article(*, article_id: int, organization_id: int | None = None) -> Decimal:
    facts = PaymentFact.objects.filter(
        article_id=article_id,
        accounting_kind="bu",
        direction=PaymentDirection.OUTFLOW,
    ).select_related("currency", "contract")
    if organization_id:
        facts = facts.filter(organization_id=organization_id)
    total = Decimal("0")
    for fact in facts:
        total += amount_to_rub(
            currency_code=fact.currency.code,
            amount=fact.amount,
            manual_exchange_rate=(fact.contract.manual_exchange_rate if fact.contract else None),
        )
    return quant_money(total)


def reserved_amount_rub_by_article(
    *, article_id: int, organization_id: int | None = None, exclude_request_id: int | None = None
) -> Decimal:
    query = PaymentRequest.objects.filter(article_id=article_id, status__in=RESERVED_REQUEST_STATUSES)
    if organization_id:
        query = query.filter(organization_id=organization_id)
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
    return next_document_number("REQ")


def next_do_external_id() -> str:
    return next_document_number("DO")


def _validate_request_payload(
    *,
    request_kind: str,
    counterparty,
    contract: Contract | None,
    additional_agreement: AdditionalAgreement | None,
    invoice_number: str,
    invoice_date,
    payment_purpose: str,
    justification_text: str,
    amount: Decimal,
    manual_exchange_rate: Decimal | None,
) -> None:
    if request_kind not in PaymentRequestKind.values:
        raise ValueError("Неизвестный тип заявки")
    if amount is None:
        raise ValueError("Укажите сумму заявки")
    if amount <= 0:
        raise ValueError("Сумма заявки должна быть больше нуля")
    if not payment_purpose.strip():
        raise ValueError("Укажите назначение платежа")

    if request_kind == PaymentRequestKind.BY_CONTRACT and contract is None:
        raise ValueError("Для заявки по договору выберите договор")
    if request_kind == PaymentRequestKind.BY_INVOICE and not invoice_number.strip():
        raise ValueError("Для заявки по счету укажите номер счета")
    if request_kind == PaymentRequestKind.BY_INVOICE and invoice_date is None:
        raise ValueError("Для заявки по счету укажите дату счета")
    if request_kind == PaymentRequestKind.WITHOUT_CONTRACT and contract is not None:
        raise ValueError("Для заявки без договора поле договора должно быть пустым")
    if request_kind == PaymentRequestKind.WITHOUT_CONTRACT and not justification_text.strip():
        raise ValueError("Для заявки без договора заполните обоснование оплаты")

    if contract is not None:
        if contract.kind != ContractKind.SOLE_SUPPLIER:
            raise ValueError("В заявке можно использовать только договор поставщика")
        if contract.counterparty_id != counterparty.id:
            raise ValueError("Контрагент заявки должен совпадать с контрагентом договора")

    if additional_agreement is not None:
        if contract is None:
            raise ValueError("Допсоглашение можно выбрать только при выборе договора")
        if additional_agreement.contract_id != contract.id:
            raise ValueError("Выбранное допсоглашение должно принадлежать договору заявки")

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
