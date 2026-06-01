"""Variance Analysis — автоматический анализ отклонений «план vs факт».

Для каждой статьи ДДС со значительным отклонением раскладывает причину
по контрагентам: «перерасход 500k по DDS-010 на 60% из-за контрагента X,
на 40% из-за Y».

Это enterprise-фишка SAP/Oracle: финансовому директору не надо вручную
копать в каждую статью — система сразу показывает top-N драйверов
отклонения и их вклад.
"""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Sum

from core.models import (
    BudgetLimitAdjustment,
    BudgetLimitPlan,
    BudgetPlanStatus,
    CashFlowArticle,
    PaymentDirection,
    PaymentFact,
)


MONEY_ZERO = Decimal("0.00")


@dataclass
class VarianceDriver:
    """Один драйвер отклонения по статье — контрагент с его вкладом."""
    counterparty_name: str
    fact_amount: Decimal
    contribution_pct: Decimal  # доля этого контрагента в общем факте по статье


@dataclass
class ArticleVariance:
    """Отклонение по конкретной статье."""
    article: CashFlowArticle
    plan_amount: Decimal
    fact_amount: Decimal
    variance: Decimal
    variance_pct: Decimal
    is_overrun: bool  # факт > план
    severity: str  # critical / warning / info
    top_drivers: list[VarianceDriver]


def build_variance_analysis(
    *,
    year: int,
    organization_id: int | None = None,
    threshold_pct: Decimal = Decimal("10"),  # отклонения < 10% игнорируем
    top_n: int = 10,
) -> dict:
    """Анализ отклонений плана от факта за год по статьям ДДС.

    threshold_pct — минимальное отклонение в %, чтобы статья попала в отчёт.
    top_n — сколько самых проблемных статей вывести.
    """
    # План: сумма approved BudgetLimitPlan + последняя approved корректировка
    plans_q = BudgetLimitPlan.objects.filter(
        status=BudgetPlanStatus.APPROVED,
        planning_year=year,
    )
    if organization_id:
        plans_q = plans_q.filter(organization_id=organization_id)

    plans_by_article: dict[int, Decimal] = defaultdict(lambda: MONEY_ZERO)
    for row in plans_q.values("article_id").annotate(total=Sum("annual_amount")):
        plans_by_article[row["article_id"]] = row["total"] or MONEY_ZERO

    # Корректировки: для каждого base_plan берём максимальную (последнюю по версии) approved
    adjustments = (
        BudgetLimitAdjustment.objects
        .filter(
            status=BudgetPlanStatus.APPROVED,
            base_plan__planning_year=year,
        )
        .select_related("base_plan")
    )
    if organization_id:
        adjustments = adjustments.filter(base_plan__organization_id=organization_id)
    adjustment_delta: dict[int, Decimal] = defaultdict(lambda: MONEY_ZERO)
    seen_base_plan_ids: set[int] = set()
    for adj in adjustments.order_by("base_plan_id", "-version"):
        if adj.base_plan_id in seen_base_plan_ids:
            continue
        seen_base_plan_ids.add(adj.base_plan_id)
        delta = adj.new_annual_amount - adj.base_plan.annual_amount
        adjustment_delta[adj.article_id] += delta

    # Факт: сумма outflow PaymentFact за год по статье (БУ)
    facts_q = PaymentFact.objects.filter(
        date__year=year,
        direction=PaymentDirection.OUTFLOW,
        accounting_kind="bu",
    )
    if organization_id:
        facts_q = facts_q.filter(organization_id=organization_id)

    facts_by_article: dict[int, Decimal] = defaultdict(lambda: MONEY_ZERO)
    for row in facts_q.values("article_id").annotate(total=Sum("amount")):
        facts_by_article[row["article_id"]] = row["total"] or MONEY_ZERO

    # Драйверы по контрагентам: сумма факта по (article, counterparty)
    drivers_raw = (
        facts_q.values("article_id", "counterparty__name")
        .annotate(total=Sum("amount"))
    )
    drivers_by_article: dict[int, list[tuple[str, Decimal]]] = defaultdict(list)
    for row in drivers_raw:
        drivers_by_article[row["article_id"]].append(
            (row["counterparty__name"] or "—", row["total"] or MONEY_ZERO)
        )

    # Собираем variance по каждой статье где есть план ИЛИ факт
    article_ids = set(plans_by_article.keys()) | set(facts_by_article.keys())
    variances: list[ArticleVariance] = []
    articles_map = {a.id: a for a in CashFlowArticle.objects.filter(id__in=article_ids)}

    for art_id in article_ids:
        article = articles_map.get(art_id)
        if article is None:
            continue
        plan = plans_by_article.get(art_id, MONEY_ZERO) + adjustment_delta.get(art_id, MONEY_ZERO)
        fact = facts_by_article.get(art_id, MONEY_ZERO)
        variance = fact - plan
        if plan == 0 and fact == 0:
            continue
        variance_pct = (
            (variance / plan * Decimal("100")).quantize(Decimal("0.1"))
            if plan > 0
            else Decimal("100") if fact > 0 else Decimal("0")
        )
        # Фильтр по threshold (abs %)
        if abs(variance_pct) < threshold_pct and plan > 0:
            continue

        # Severity
        abs_pct = abs(variance_pct)
        if abs_pct >= 50:
            severity = "critical"
        elif abs_pct >= 25:
            severity = "warning"
        else:
            severity = "info"

        # Top-3 драйвера по этой статье
        drivers_raw_list = sorted(drivers_by_article.get(art_id, []), key=lambda x: -x[1])[:3]
        top_drivers = []
        for name, amount in drivers_raw_list:
            pct = (
                (amount / fact * Decimal("100")).quantize(Decimal("0.1"))
                if fact > 0 else Decimal("0")
            )
            top_drivers.append(VarianceDriver(
                counterparty_name=name,
                fact_amount=amount,
                contribution_pct=pct,
            ))

        variances.append(ArticleVariance(
            article=article,
            plan_amount=plan,
            fact_amount=fact,
            variance=variance,
            variance_pct=variance_pct,
            is_overrun=variance > 0,
            severity=severity,
            top_drivers=top_drivers,
        ))

    # Сортировка по убыванию |variance| — самые проблемные сверху
    variances.sort(key=lambda v: -abs(v.variance))

    # Агрегаты для headline
    total_overrun = sum((v.variance for v in variances if v.is_overrun), MONEY_ZERO)
    total_savings = sum((v.variance for v in variances if not v.is_overrun), MONEY_ZERO)
    critical_count = sum(1 for v in variances if v.severity == "critical")
    warning_count = sum(1 for v in variances if v.severity == "warning")

    return {
        "year": year,
        "organization_id": organization_id,
        "threshold_pct": threshold_pct,
        "variances": variances[:top_n],
        "variances_count": len(variances),
        "total_overrun": total_overrun,
        "total_savings": total_savings,
        "net_variance": total_overrun + total_savings,
        "critical_count": critical_count,
        "warning_count": warning_count,
    }
