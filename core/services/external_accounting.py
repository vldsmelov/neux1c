from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from django.contrib.auth import get_user_model

from core.models import (
    AccountingKind,
    AuditAction,
    AuditLog,
    CashFlowArticle,
    Contract,
    ExternalPaymentDocument,
    NotificationKind,
    Organization,
    PaymentDirection,
    PaymentFact,
    SourceSystem,
    UserRole,
)
from core.services.document_numbers import next_document_number
from core.services.notifications import notify_many


DEFAULT_PAYMENT_ACCOUNT = "51"


@transaction.atomic
def pay_contract(contract: Contract, accountant, amount: Decimal | None = None) -> ExternalPaymentDocument:
    amount = amount or remaining_contract_amount(contract)
    if amount <= 0:
        raise ValueError("Сумма оплаты должна быть больше нуля")

    payment = ExternalPaymentDocument.objects.create(
        number=next_payment_number(),
        contract=contract,
        accountant=accountant,
        payment_date=timezone.localdate(),
        amount=amount,
        currency=contract.currency,
        comment="Оплата во внешнем контуре 1С:БП",
    )

    bu_fact = _create_payment_fact(payment, AccountingKind.BU)
    nu_fact = _create_payment_fact(payment, AccountingKind.NU)
    payment.payment_fact_bu = bu_fact
    payment.payment_fact_nu = nu_fact
    payment.save(update_fields=["payment_fact_bu", "payment_fact_nu"])

    AuditLog.objects.create(
        user=accountant,
        action=AuditAction.PAY,
        path="/external/accounting/",
        method="POST",
        status_code=200,
        object_type="ExternalPaymentDocument",
        object_id=str(payment.pk),
        message=f"Оплачен договор {contract.number} на сумму {amount}",
    )

    # Уведомить экономистов: оплата прошла через бухгалтерский контур —
    # факт зайдёт в БДДС, лимит уже потрачен и резерв изменится.
    User = get_user_model()
    economists = list(User.objects.filter(profile__role=UserRole.ECONOMIST, is_active=True))
    notify_many(
        economists,
        kind=NotificationKind.EXTERNAL_PAYMENT_POSTED,
        title=f"Внешняя оплата {payment.number}",
        text=f"Договор {contract.number} · {amount} {contract.currency.code}",
        link="/payments/facts/",
        related=payment,
    )
    return payment


def paid_amount_for_contract(contract: Contract) -> Decimal:
    return (
        ExternalPaymentDocument.objects.filter(contract=contract)
        .aggregate(total=Sum("amount"))
        .get("total")
        or Decimal("0")
    )


def remaining_contract_amount(contract: Contract) -> Decimal:
    return max(contract.reserved_amount - paid_amount_for_contract(contract), Decimal("0"))


def next_payment_number() -> str:
    return next_document_number("BP")


def _create_payment_fact(payment: ExternalPaymentDocument, accounting_kind: str) -> PaymentFact:
    now = timezone.now()
    external_id = f"external-payment-{payment.pk}"
    return PaymentFact.objects.create(
        external_id=external_id,
        source_system=SourceSystem.ONE_C_BP,
        synced_at=now,
        organization=_default_organization(),
        date=payment.payment_date,
        account=DEFAULT_PAYMENT_ACCOUNT,
        direction=PaymentDirection.OUTFLOW,
        accounting_kind=accounting_kind,
        article=_default_payment_article(),
        counterparty=payment.contract.counterparty,
        contract=payment.contract,
        amount=payment.amount,
        currency=payment.currency,
        comment=f"{payment.number}: оплата договора во внешней системе",
    )


def _default_organization() -> Organization:
    organization = Organization.objects.order_by("id").first()
    if not organization:
        raise ValueError("Для оплаты нужна организация. Запустите sync_mock_1c.")
    return organization


def _default_payment_article() -> CashFlowArticle:
    article = CashFlowArticle.objects.filter(code="DDS-010").first() or CashFlowArticle.objects.order_by("id").first()
    if not article:
        raise ValueError("Для оплаты нужна статья ДДС. Запустите sync_mock_1c.")
    return article
