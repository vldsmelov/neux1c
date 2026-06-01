"""Intercompany Elimination & Consolidation Report.

Считает консолидированный отчёт по всему холдингу:
* По каждой компании-члену холдинга: cash in / cash out / net (БДДС-метод).
* Отдельной строкой: ВГО — внутригрупповые обороты (по статьям
  is_internal_turnover=True), которые устраняются при консолидации.
* Итог: чистая картина холдинга после elimination.

Это базовая enterprise-функция consolidation engine (как в SAP BPC,
Oracle Hyperion, Workday). В упрощённой форме без mapping pairs:
ВГО определяется через флаг на статье ДДС.
"""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Sum

from core.models import (
    Organization,
    PaymentDirection,
    PaymentFact,
)


MONEY_ZERO = Decimal("0.00")


@dataclass
class CompanyConsolidation:
    organization: Organization
    cash_in: Decimal
    cash_out: Decimal
    cash_in_external: Decimal  # без ВГО
    cash_out_external: Decimal  # без ВГО
    cash_in_internal: Decimal  # только ВГО
    cash_out_internal: Decimal  # только ВГО
    net: Decimal  # gross net = in − out
    net_external: Decimal  # после elimination


def build_consolidation_report(
    *, year: int, include_external: bool = False
) -> dict:
    """Консолидированная сводка по всем компаниям-членам холдинга за год.

    include_external: если True, добавляем строкой внешние компании
    (тоже видны, но отдельной секцией — для сравнения).
    """
    holding_q = Organization.objects.filter(is_holding_member=True)
    holding_ids = list(holding_q.values_list("id", flat=True))

    facts_q = PaymentFact.objects.filter(
        date__year=year,
        organization_id__in=holding_ids,
        accounting_kind="bu",
    ).select_related("organization", "article")

    # Группируем: (org_id, direction, is_internal) → sum
    by_company: dict[int, dict] = defaultdict(lambda: {
        "cash_in": MONEY_ZERO, "cash_out": MONEY_ZERO,
        "cash_in_internal": MONEY_ZERO, "cash_out_internal": MONEY_ZERO,
    })

    aggregated = (
        facts_q
        .values("organization_id", "direction", "article__is_internal_turnover")
        .annotate(t=Sum("amount"))
    )
    for row in aggregated:
        bucket = by_company[row["organization_id"]]
        direction = row["direction"]
        is_internal = row["article__is_internal_turnover"]
        amount = row["t"] or MONEY_ZERO
        if direction == PaymentDirection.INFLOW:
            bucket["cash_in"] += amount
            if is_internal:
                bucket["cash_in_internal"] += amount
        else:
            bucket["cash_out"] += amount
            if is_internal:
                bucket["cash_out_internal"] += amount

    companies = []
    org_map = {o.id: o for o in holding_q}
    for org_id in holding_ids:
        b = by_company.get(org_id, None)
        org = org_map[org_id]
        if b is None:
            companies.append(CompanyConsolidation(
                organization=org,
                cash_in=MONEY_ZERO, cash_out=MONEY_ZERO,
                cash_in_external=MONEY_ZERO, cash_out_external=MONEY_ZERO,
                cash_in_internal=MONEY_ZERO, cash_out_internal=MONEY_ZERO,
                net=MONEY_ZERO, net_external=MONEY_ZERO,
            ))
            continue
        cash_in = b["cash_in"]
        cash_out = b["cash_out"]
        cash_in_int = b["cash_in_internal"]
        cash_out_int = b["cash_out_internal"]
        cash_in_ext = cash_in - cash_in_int
        cash_out_ext = cash_out - cash_out_int
        companies.append(CompanyConsolidation(
            organization=org,
            cash_in=cash_in, cash_out=cash_out,
            cash_in_external=cash_in_ext, cash_out_external=cash_out_ext,
            cash_in_internal=cash_in_int, cash_out_internal=cash_out_int,
            net=cash_in - cash_out,
            net_external=cash_in_ext - cash_out_ext,
        ))

    # Итоги
    total_gross_in = sum((c.cash_in for c in companies), MONEY_ZERO)
    total_gross_out = sum((c.cash_out for c in companies), MONEY_ZERO)
    total_eliminated_in = sum((c.cash_in_internal for c in companies), MONEY_ZERO)
    total_eliminated_out = sum((c.cash_out_internal for c in companies), MONEY_ZERO)
    total_consolidated_in = total_gross_in - total_eliminated_in
    total_consolidated_out = total_gross_out - total_eliminated_out
    total_consolidated_net = total_consolidated_in - total_consolidated_out

    # ВГО симметрия check: в идеале cash_in_internal по всему холдингу
    # = cash_out_internal (что один платит, другой получает). Разница
    # — индикатор пропущенных проводок.
    elimination_imbalance = total_eliminated_in - total_eliminated_out

    return {
        "year": year,
        "companies": companies,
        "holding_size": len(companies),
        "total_gross_in": total_gross_in,
        "total_gross_out": total_gross_out,
        "total_eliminated_in": total_eliminated_in,
        "total_eliminated_out": total_eliminated_out,
        "total_consolidated_in": total_consolidated_in,
        "total_consolidated_out": total_consolidated_out,
        "total_consolidated_net": total_consolidated_net,
        "elimination_imbalance": elimination_imbalance,
    }
