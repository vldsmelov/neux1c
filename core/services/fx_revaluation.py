"""FX Revaluation — переоценка валютных позиций по текущему курсу.

Для каждого активного валютного контракта считаем:
* Original amount: сумма в валюте контракта.
* RUB at signing: сумма × курс на дату подписания.
* RUB at revaluation date: сумма × курс на дату переоценки.
* FX gain/loss: разница (положительная — прибыль, отрицательная — убыток).

Применимо к открытым позициям (договоры в работе с непогашенным остатком).
В корпоративной практике это считают на конец каждого месяца и
признают курсовую разницу в P&L.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date as date_cls
from decimal import Decimal

from core.models import Contract
from core.services.exchange_rates import get_rate_to_rub


MONEY_ZERO = Decimal("0.00")
RUB_CODE = "RUB"


@dataclass
class FxPosition:
    contract: Contract
    amount_foreign: Decimal
    rate_at_signing: Decimal
    rate_at_revaluation: Decimal
    rub_at_signing: Decimal
    rub_at_revaluation: Decimal
    fx_diff: Decimal
    rate_change_pct: Decimal


def build_fx_revaluation(*, revaluation_date: date_cls, organization_id: int | None = None) -> dict:
    """Сводка переоценки на дату по всем валютным контрактам."""
    contracts_q = Contract.objects.exclude(currency__code=RUB_CODE).select_related(
        "currency", "counterparty", "funding_case"
    )
    if organization_id:
        # Привязка к организации идёт через funding_case
        contracts_q = contracts_q.filter(funding_case__organization_id=organization_id)

    positions: list[FxPosition] = []
    fx_total_gain = MONEY_ZERO
    fx_total_loss = MONEY_ZERO
    by_currency: dict[str, dict] = defaultdict(lambda: {
        "total_foreign": MONEY_ZERO,
        "total_rub_signing": MONEY_ZERO,
        "total_rub_now": MONEY_ZERO,
        "fx_diff": MONEY_ZERO,
        "count": 0,
    })
    missing_rates: list[Contract] = []

    for c in contracts_q:
        rate_signing = get_rate_to_rub(c.currency, c.date)
        rate_now = get_rate_to_rub(c.currency, revaluation_date)
        if rate_signing is None or rate_now is None:
            missing_rates.append(c)
            continue

        amount = Decimal(c.amount)
        rub_signing = (amount * rate_signing).quantize(Decimal("0.01"))
        rub_now = (amount * rate_now).quantize(Decimal("0.01"))
        diff = rub_now - rub_signing
        change_pct = (
            (rate_now - rate_signing) / rate_signing * Decimal("100")
        ).quantize(Decimal("0.01")) if rate_signing else Decimal("0")

        positions.append(FxPosition(
            contract=c,
            amount_foreign=amount,
            rate_at_signing=rate_signing,
            rate_at_revaluation=rate_now,
            rub_at_signing=rub_signing,
            rub_at_revaluation=rub_now,
            fx_diff=diff,
            rate_change_pct=change_pct,
        ))

        bucket = by_currency[c.currency.code]
        bucket["total_foreign"] += amount
        bucket["total_rub_signing"] += rub_signing
        bucket["total_rub_now"] += rub_now
        bucket["fx_diff"] += diff
        bucket["count"] += 1

        if diff > 0:
            fx_total_gain += diff
        else:
            fx_total_loss += diff

    # Кладём rate_now последний в bucket для display
    for code, bucket in by_currency.items():
        rate = next((p.rate_at_revaluation for p in positions if p.contract.currency.code == code), None)
        bucket["rate_now"] = rate
        bucket["code"] = code

    positions.sort(key=lambda p: p.fx_diff)  # худшие убытки сверху

    return {
        "revaluation_date": revaluation_date,
        "positions": positions,
        "by_currency": list(by_currency.values()),
        "fx_total_gain": fx_total_gain,
        "fx_total_loss": fx_total_loss,
        "fx_net": fx_total_gain + fx_total_loss,
        "positions_count": len(positions),
        "missing_rates": missing_rates,
    }
