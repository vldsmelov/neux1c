import csv
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from io import StringIO

from django.db.models import Max, Sum

from core.models import (
    AccountingKind,
    BudgetLimitAdjustment,
    BudgetLimitPlan,
    BudgetPlanStatus,
    CashFlowArticle,
    Contract,
    ContractKind,
    PaymentDirection,
    PaymentFact,
    PaymentRequest,
    PaymentRequestStatus,
)
from core.services.payment_requests import quant_money


RUB_CODE = "RUB"
MONEY_ZERO = Decimal("0.00")
DEFAULT_REQUEST_STATUSES = (
    PaymentRequestStatus.PENDING_APPROVAL,
    PaymentRequestStatus.APPROVED,
    PaymentRequestStatus.TRANSFERRED,
)


@dataclass(frozen=True)
class PlanFactFilters:
    year: int
    article_id: int | None = None
    counterparty_id: int | None = None
    request_status: str = ""

    @property
    def request_statuses(self) -> tuple[str, ...]:
        if self.request_status in PaymentRequestStatus.values:
            return (self.request_status,)
        return DEFAULT_REQUEST_STATUSES


def build_plan_fact_report(filters: PlanFactFilters) -> tuple[list[dict], dict]:
    articles_query = CashFlowArticle.objects.order_by("code")
    if filters.article_id:
        articles_query = articles_query.filter(pk=filters.article_id)
    articles = list(articles_query)
    if not articles:
        return [], _empty_summary()

    article_ids = [article.id for article in articles]
    plans_by_article = _plans_by_article(article_ids, filters.year)
    adjustments_by_article = _adjustments_by_article(article_ids, filters.year)
    reserved_by_article = _reserved_by_article(article_ids, filters.year, filters.counterparty_id)
    requested_by_article = _requested_by_article(article_ids, filters)
    fact_bu_by_article = _facts_by_article(article_ids, filters.year, filters.counterparty_id, AccountingKind.BU)
    fact_nu_by_article = _facts_by_article(article_ids, filters.year, filters.counterparty_id, AccountingKind.NU)

    rows = []
    for article in articles:
        plan_amount = plans_by_article.get(article.id, MONEY_ZERO)
        adjustments_amount = adjustments_by_article.get(article.id, MONEY_ZERO)
        reserved_amount = reserved_by_article.get(article.id, MONEY_ZERO)
        requested_amount = requested_by_article.get(article.id, MONEY_ZERO)
        fact_bu = fact_bu_by_article.get(article.id, MONEY_ZERO)
        fact_nu = fact_nu_by_article.get(article.id, MONEY_ZERO)
        balance_bu = quant_money(plan_amount + adjustments_amount - reserved_amount - requested_amount - fact_bu)
        balance_nu = quant_money(plan_amount + adjustments_amount - reserved_amount - requested_amount - fact_nu)

        rows.append(
            {
                "article": article,
                "plan": plan_amount,
                "adjustments": adjustments_amount,
                "reserved": reserved_amount,
                "requested": requested_amount,
                "fact_bu": fact_bu,
                "fact_nu": fact_nu,
                "balance_bu": balance_bu,
                "balance_nu": balance_nu,
                "facts_gap": quant_money(fact_bu - fact_nu),
                "has_limit_overrun": balance_bu < 0,
                "is_internal_turnover": article.is_internal_turnover,
                "missing_in_one_c": not article.exists_in_one_c,
            }
        )

    if filters.counterparty_id and filters.article_id is None:
        rows = [row for row in rows if _row_has_values(row)]

    summary = {
        "articles": len(rows),
        "total_plan": quant_money(sum((row["plan"] for row in rows), MONEY_ZERO)),
        "total_adjustments": quant_money(sum((row["adjustments"] for row in rows), MONEY_ZERO)),
        "total_reserved": quant_money(sum((row["reserved"] for row in rows), MONEY_ZERO)),
        "total_requested": quant_money(sum((row["requested"] for row in rows), MONEY_ZERO)),
        "total_fact_bu": quant_money(sum((row["fact_bu"] for row in rows), MONEY_ZERO)),
        "total_fact_nu": quant_money(sum((row["fact_nu"] for row in rows), MONEY_ZERO)),
        "total_balance_bu": quant_money(sum((row["balance_bu"] for row in rows), MONEY_ZERO)),
        "total_balance_nu": quant_money(sum((row["balance_nu"] for row in rows), MONEY_ZERO)),
        "overrun_articles": sum(1 for row in rows if row["has_limit_overrun"]),
        "vgo_articles": sum(1 for row in rows if row["is_internal_turnover"]),
    }
    return rows, summary


def to_csv(rows: list[dict]) -> str:
    output = StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(
        [
            "Статья ДДС",
            "Признаки",
            "План",
            "Корректировка лимитов",
            "Резервирование",
            "Заявлено к оплате",
            "Факт БУ",
            "Факт НУ",
            "Остаток (БУ)",
            "Остаток (НУ)",
        ]
    )
    for row in rows:
        features = []
        if row["missing_in_one_c"]:
            features.append("нет в 1С")
        if row["is_internal_turnover"]:
            features.append("ВГО")
        writer.writerow(
            [
                f"{row['article'].code} {row['article'].name}",
                ", ".join(features) if features else "из 1С",
                row["plan"],
                row["adjustments"],
                row["reserved"],
                row["requested"],
                row["fact_bu"],
                row["fact_nu"],
                row["balance_bu"],
                row["balance_nu"],
            ]
        )
    return "\ufeff" + output.getvalue()


def _plans_by_article(article_ids: list[int], year: int) -> dict[int, Decimal]:
    return {
        row["article_id"]: row["total"] or MONEY_ZERO
        for row in BudgetLimitPlan.objects.filter(
            article_id__in=article_ids,
            status=BudgetPlanStatus.APPROVED,
            planning_year=year,
        )
        .values("article_id")
        .annotate(total=Sum("annual_amount"))
    }


def _adjustments_by_article(article_ids: list[int], year: int) -> dict[int, Decimal]:
    latest_ids = {
        row["base_plan_id"]: row["latest_id"]
        for row in BudgetLimitAdjustment.objects.filter(
            article_id__in=article_ids,
            status=BudgetPlanStatus.APPROVED,
            base_plan__planning_year=year,
        )
        .values("base_plan_id")
        .annotate(latest_id=Max("id"))
    }
    adjustments = (
        BudgetLimitAdjustment.objects.filter(id__in=latest_ids.values())
        .select_related("base_plan")
        .only("article_id", "new_annual_amount", "base_plan__annual_amount")
    )
    result = defaultdict(lambda: MONEY_ZERO)
    for adjustment in adjustments:
        delta = quant_money(adjustment.new_annual_amount - adjustment.base_plan.annual_amount)
        result[adjustment.article_id] = quant_money(result[adjustment.article_id] + delta)
    return dict(result)


def _reserved_by_article(article_ids: list[int], year: int, counterparty_id: int | None) -> dict[int, Decimal]:
    contracts = Contract.objects.filter(
        kind=ContractKind.SOLE_SUPPLIER,
        date__year=year,
    ).select_related("currency")
    if counterparty_id:
        contracts = contracts.filter(counterparty_id=counterparty_id)
    contracts = list(contracts)
    if not contracts:
        return {}

    contract_ids = [contract.id for contract in contracts]
    latest_facts = {
        row["contract_id"]: row["latest_id"]
        for row in PaymentFact.objects.filter(
            contract_id__in=contract_ids,
            accounting_kind=AccountingKind.BU,
            direction=PaymentDirection.OUTFLOW,
            date__year=year,
        )
        .values("contract_id")
        .annotate(latest_id=Max("id"))
    }
    if not latest_facts:
        return {}

    article_map = {
        fact.contract_id: fact.article_id
        for fact in PaymentFact.objects.filter(id__in=latest_facts.values()).only("contract_id", "article_id")
    }
    result = defaultdict(lambda: MONEY_ZERO)
    for contract in contracts:
        article_id = article_map.get(contract.id)
        if article_id is None or article_id not in article_ids:
            continue
        reserved_rub = _to_rub(contract.currency.code, contract.reserved_amount, contract.manual_exchange_rate)
        result[article_id] = quant_money(result[article_id] + reserved_rub)
    return dict(result)


def _requested_by_article(article_ids: list[int], filters: PlanFactFilters) -> dict[int, Decimal]:
    query = PaymentRequest.objects.filter(
        article_id__in=article_ids,
        request_date__year=filters.year,
        status__in=filters.request_statuses,
    )
    if filters.counterparty_id:
        query = query.filter(counterparty_id=filters.counterparty_id)
    return {
        row["article_id"]: row["total"] or MONEY_ZERO
        for row in query.values("article_id").annotate(total=Sum("amount_rub"))
    }


def _facts_by_article(
    article_ids: list[int],
    year: int,
    counterparty_id: int | None,
    accounting_kind: str,
) -> dict[int, Decimal]:
    query = PaymentFact.objects.filter(
        article_id__in=article_ids,
        date__year=year,
        accounting_kind=accounting_kind,
        direction=PaymentDirection.OUTFLOW,
    ).select_related("currency", "contract")
    if counterparty_id:
        query = query.filter(counterparty_id=counterparty_id)

    result = defaultdict(lambda: MONEY_ZERO)
    for fact in query:
        rate = fact.contract.manual_exchange_rate if fact.contract_id else None
        amount_rub = _to_rub(fact.currency.code, fact.amount, rate)
        result[fact.article_id] = quant_money(result[fact.article_id] + amount_rub)
    return dict(result)


def _to_rub(currency_code: str, amount: Decimal, manual_exchange_rate: Decimal | None) -> Decimal:
    if currency_code == RUB_CODE:
        return quant_money(amount)
    rate = manual_exchange_rate or Decimal("0")
    if rate <= 0:
        return quant_money(amount)
    return quant_money(amount * rate)


def _row_has_values(row: dict) -> bool:
    return any(
        row[field] != MONEY_ZERO
        for field in ("plan", "adjustments", "reserved", "requested", "fact_bu", "fact_nu", "balance_bu", "balance_nu")
    )


def _empty_summary() -> dict:
    return {
        "articles": 0,
        "total_plan": MONEY_ZERO,
        "total_adjustments": MONEY_ZERO,
        "total_reserved": MONEY_ZERO,
        "total_requested": MONEY_ZERO,
        "total_fact_bu": MONEY_ZERO,
        "total_fact_nu": MONEY_ZERO,
        "total_balance_bu": MONEY_ZERO,
        "total_balance_nu": MONEY_ZERO,
        "overrun_articles": 0,
        "vgo_articles": 0,
    }
