from decimal import Decimal

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from core.models import (
    AuditAction,
    AuditLog,
    PaymentFact,
    PaymentFactAdjustment,
    PaymentFactControlSettings,
)


WORKFLOW_PATH = "/payments/facts/"
MONEY_QUANT = Decimal("0.01")


@transaction.atomic
def adjust_payment_fact(
    *,
    payment_fact: PaymentFact,
    author,
    new_amount: Decimal,
    reason: str,
    new_comment: str = "",
) -> PaymentFactAdjustment:
    if new_amount is None:
        raise ValueError("Укажите новую сумму факта")
    if new_amount < 0:
        raise ValueError("Сумма факта не может быть отрицательной")
    if not reason.strip():
        raise ValueError("Причина корректировки обязательна")

    settings = PaymentFactControlSettings.active_or_default()
    if settings.closed_through and payment_fact.date <= settings.closed_through:
        raise ValueError(
            f"Период до {settings.closed_through:%d.%m.%Y} закрыт в 1С:БП. Корректировка недоступна."
        )

    previous_amount = payment_fact.amount
    previous_comment = payment_fact.comment
    normalized_new_amount = new_amount.quantize(MONEY_QUANT)
    normalized_new_comment = new_comment.strip()

    if normalized_new_amount == previous_amount and normalized_new_comment == previous_comment:
        raise ValueError("Изменений нет: сумма и комментарий совпадают с текущими значениями")

    last_version = (
        payment_fact.adjustments.aggregate(last_version=Max("version")).get("last_version")
        or 0
    )
    adjustment = PaymentFactAdjustment.objects.create(
        payment_fact=payment_fact,
        version=last_version + 1,
        previous_amount=previous_amount,
        new_amount=normalized_new_amount,
        previous_comment=previous_comment,
        new_comment=normalized_new_comment,
        reason=reason.strip(),
        author=author,
    )

    payment_fact.amount = normalized_new_amount
    payment_fact.comment = normalized_new_comment
    payment_fact.synced_at = timezone.now()
    payment_fact.save(update_fields=["amount", "comment", "synced_at"])

    AuditLog.objects.create(
        user=author,
        action=AuditAction.UPDATE,
        path=WORKFLOW_PATH,
        object_type="PaymentFact",
        object_id=str(payment_fact.pk),
        message=(
            f"Корректировка факта {payment_fact.id} v{adjustment.version}: "
            f"{previous_amount} -> {normalized_new_amount}"
        ),
    )
    return adjustment
