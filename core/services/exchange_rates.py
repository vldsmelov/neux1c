"""Сервис работы с курсами валют. Используется для:
* перевода валютных сумм в RUB по курсу на дату документа;
* пересчёта остатков (FX revaluation) при изменении курса;
* отображения RUB-эквивалента валютных позиций в кейсах.
"""

from datetime import date as date_cls
from decimal import Decimal

from core.models import Currency, ExchangeRate


RUB_CODE = "RUB"


def get_rate_to_rub(currency: Currency | str, target_date: date_cls) -> Decimal | None:
    """Курс валюты к RUB на указанную дату.

    Логика:
    * RUB → 1 (trivial).
    * Иначе ищем последний курс с rate_date <= target_date.
    * Если нет ни одного курса — возвращаем None (вызывающий код
      должен сам решить fallback: использовать manual_exchange_rate
      контракта или 1).
    """
    code = currency.code if isinstance(currency, Currency) else currency
    if code == RUB_CODE:
        return Decimal("1.000000")

    qs = ExchangeRate.objects.filter(
        currency__code=code,
        rate_date__lte=target_date,
    ).order_by("-rate_date", "-id")
    rate = qs.first()
    return rate.rate_to_rub if rate else None


def convert_to_rub(amount: Decimal, currency: Currency | str, target_date: date_cls, fallback_rate: Decimal | None = None) -> Decimal:
    """Сумма × курс. Если курса нет — используем fallback_rate, иначе сумма как есть."""
    rate = get_rate_to_rub(currency, target_date)
    if rate is None:
        rate = fallback_rate or Decimal("1")
    return (Decimal(amount) * rate).quantize(Decimal("0.01"))


def latest_rates_summary() -> list[dict]:
    """Сводка: последний курс для каждой валюты ≠ RUB. Для UI."""
    summary: list[dict] = []
    for cur in Currency.objects.exclude(code=RUB_CODE).order_by("code"):
        latest = (
            ExchangeRate.objects
            .filter(currency=cur)
            .order_by("-rate_date", "-id")
            .first()
        )
        summary.append({
            "currency": cur,
            "latest_rate": latest,
        })
    return summary
