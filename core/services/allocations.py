"""Allocations Engine — распределение расходов между ЦФО.

Принимает правило (AllocationRule) и набор фактов по source_article;
для каждого факта рассчитывает виртуальное разделение по целевым ЦФО.
Не пишет в БД — preview-режим. В production: можно создавать
парные «обратные» факты (списать с source ЦФО, начислить на target).
"""

from dataclasses import dataclass
from datetime import date as date_cls
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Sum

from core.models import (
    AllocationRule,
    Department,
    PaymentDirection,
    PaymentFact,
)


MONEY_ZERO = Decimal("0.00")


@dataclass
class AllocationSlice:
    department: Department
    pct: Decimal
    amount: Decimal


@dataclass
class AllocationPreview:
    rule: AllocationRule
    period_start: date_cls
    period_end: date_cls
    fact_total: Decimal
    facts_count: int
    slices: list[AllocationSlice]
    warnings: list[str]


def preview_allocation(rule: AllocationRule, period_start: date_cls, period_end: date_cls) -> AllocationPreview:
    """Превью: как разложится факт по source_article за период по правилу."""
    warnings: list[str] = []
    if not rule.is_active:
        warnings.append("Правило неактивно — preview носит информационный характер.")

    facts_q = PaymentFact.objects.filter(
        organization=rule.organization,
        article=rule.source_article,
        direction=PaymentDirection.OUTFLOW,
        date__gte=period_start,
        date__lte=period_end,
    )
    fact_total = facts_q.aggregate(t=Sum("amount"))["t"] or MONEY_ZERO
    facts_count = facts_q.count()

    target_pct_map: dict[int, Decimal] = {}
    for dept_id, pct in (rule.target_pct or {}).items():
        try:
            target_pct_map[int(dept_id)] = Decimal(str(pct))
        except (TypeError, ValueError):
            warnings.append(f"Некорректный процент для dept_id={dept_id}: {pct}")

    if rule.method == AllocationRule.METHOD_EQUAL and target_pct_map:
        equal_pct = (Decimal("100") / Decimal(len(target_pct_map))).quantize(Decimal("0.01"))
        target_pct_map = {k: equal_pct for k in target_pct_map.keys()}

    total_pct = sum(target_pct_map.values(), MONEY_ZERO)
    if total_pct != Decimal("100"):
        warnings.append(f"Сумма процентов = {total_pct}, должна быть 100. Распределение пропорционально.")

    departments = Department.objects.filter(id__in=target_pct_map.keys())
    dept_by_id = {d.id: d for d in departments}

    slices: list[AllocationSlice] = []
    for dept_id, pct in target_pct_map.items():
        dept = dept_by_id.get(dept_id)
        if not dept:
            warnings.append(f"ЦФО id={dept_id} не найден в справочнике")
            continue
        if total_pct > 0:
            normalized_pct = (pct / total_pct * Decimal("100")).quantize(Decimal("0.01"))
            amount = (fact_total * pct / total_pct).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            normalized_pct = MONEY_ZERO
            amount = MONEY_ZERO
        slices.append(AllocationSlice(department=dept, pct=normalized_pct, amount=amount))

    # Фикс остатка от округления на максимальный slice
    if slices and fact_total > 0:
        diff = fact_total - sum((s.amount for s in slices), MONEY_ZERO)
        if diff != 0:
            slices[0].amount += diff

    return AllocationPreview(
        rule=rule,
        period_start=period_start,
        period_end=period_end,
        fact_total=fact_total,
        facts_count=facts_count,
        slices=slices,
        warnings=warnings,
    )
