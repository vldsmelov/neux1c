import csv
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from io import StringIO

from django.db.models import Max, Q, Sum

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
    organization_id: int | None = None
    customer_contract_id: int | None = None
    supplier_contract_id: int | None = None
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

    articles_by_id = {article.id: article for article in articles}
    article_ids = list(articles_by_id.keys())
    plans_by_article = _plans_by_article(article_ids, filters.year)
    adjustments_by_article = _adjustments_by_article(article_ids, filters.year)

    rows_by_key: dict[tuple, dict] = {}
    _accumulate_requested_rows(rows_by_key, articles_by_id, filters)
    _accumulate_fact_rows(rows_by_key, articles_by_id, filters)
    _accumulate_reserved_rows(rows_by_key, articles_by_id, filters)

    grouped_by_article = defaultdict(list)
    for row in rows_by_key.values():
        grouped_by_article[row["article"].id].append(row)

    for article in articles:
        if article.id not in grouped_by_article:
            synthetic = _build_row(
                article=article,
                organization=None,
                counterparty=None,
                customer_contract=None,
                supplier_contract=None,
            )
            grouped_by_article[article.id].append(synthetic)
            rows_by_key[synthetic["_key"]] = synthetic

    rows = []
    for article in articles:
        article_rows = grouped_by_article[article.id]
        article_rows.sort(key=_row_sort_key)

        for idx, row in enumerate(article_rows):
            row["plan"] = plans_by_article.get(article.id, MONEY_ZERO) if idx == 0 else MONEY_ZERO
            row["adjustments"] = adjustments_by_article.get(article.id, MONEY_ZERO) if idx == 0 else MONEY_ZERO
            row["balance_bu"] = quant_money(
                row["plan"] + row["adjustments"] - row["reserved"] - row["requested"] - row["fact_bu"]
            )
            row["balance_nu"] = quant_money(
                row["plan"] + row["adjustments"] - row["reserved"] - row["requested"] - row["fact_nu"]
            )
            row["facts_gap"] = quant_money(row["fact_bu"] - row["fact_nu"])
            row["has_limit_overrun"] = row["balance_bu"] < 0
            row["is_internal_turnover"] = row["article"].is_internal_turnover
            row["missing_in_one_c"] = not row["article"].exists_in_one_c
            row["comments"] = "; ".join(sorted(row["_comments"])) if row["_comments"] else ""
            del row["_comments"]
            del row["_key"]
            rows.append(row)

    unique_article_ids = {row["article"].id for row in rows}
    overrun_article_ids = {row["article"].id for row in rows if row["has_limit_overrun"]}
    vgo_article_ids = {row["article"].id for row in rows if row["is_internal_turnover"]}

    summary = {
        "articles": len(unique_article_ids),
        "total_plan": quant_money(sum((row["plan"] for row in rows), MONEY_ZERO)),
        "total_adjustments": quant_money(sum((row["adjustments"] for row in rows), MONEY_ZERO)),
        "total_reserved": quant_money(sum((row["reserved"] for row in rows), MONEY_ZERO)),
        "total_requested": quant_money(sum((row["requested"] for row in rows), MONEY_ZERO)),
        "total_fact_bu": quant_money(sum((row["fact_bu"] for row in rows), MONEY_ZERO)),
        "total_fact_nu": quant_money(sum((row["fact_nu"] for row in rows), MONEY_ZERO)),
        "total_balance_bu": quant_money(sum((row["balance_bu"] for row in rows), MONEY_ZERO)),
        "total_balance_nu": quant_money(sum((row["balance_nu"] for row in rows), MONEY_ZERO)),
        "overrun_articles": len(overrun_article_ids),
        "vgo_articles": len(vgo_article_ids),
    }
    return rows, summary


def to_csv(rows: list[dict]) -> str:
    output = StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(
        [
            "Организация",
            "Статья ДДС",
            "Контрагент",
            "Доходный договор",
            "Договор поставщика",
            "Признаки",
            "План",
            "Корректировка лимитов",
            "Резервирование",
            "Заявлено к оплате",
            "Факт БУ",
            "Факт НУ",
            "Остаток (БУ)",
            "Остаток (НУ)",
            "Комментарии",
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
                row["organization"].name if row["organization"] else "—",
                f"{row['article'].code} {row['article'].name}",
                row["counterparty"].name if row["counterparty"] else "—",
                row["customer_contract"].number if row["customer_contract"] else "—",
                row["supplier_contract"].number if row["supplier_contract"] else "—",
                ", ".join(features) if features else "из 1С",
                row["plan"],
                row["adjustments"],
                row["reserved"],
                row["requested"],
                row["fact_bu"],
                row["fact_nu"],
                row["balance_bu"],
                row["balance_nu"],
                row["comments"],
            ]
        )
    return "\ufeff" + output.getvalue()


def _accumulate_requested_rows(rows_by_key: dict, articles_by_id: dict[int, CashFlowArticle], filters: PlanFactFilters) -> None:
    query = PaymentRequest.objects.filter(
        article_id__in=articles_by_id.keys(),
        request_date__year=filters.year,
        status__in=filters.request_statuses,
    ).select_related("organization", "article", "counterparty", "contract", "contract__parent_customer_contract")
    if filters.counterparty_id:
        query = query.filter(counterparty_id=filters.counterparty_id)
    if filters.organization_id:
        query = query.filter(organization_id=filters.organization_id)
    if filters.supplier_contract_id:
        query = query.filter(contract_id=filters.supplier_contract_id)
    if filters.customer_contract_id:
        query = query.filter(
            Q(contract__id=filters.customer_contract_id)
            | Q(contract__parent_customer_contract_id=filters.customer_contract_id)
        )

    for request in query:
        supplier_contract = request.contract if request.contract and request.contract.kind == ContractKind.SOLE_SUPPLIER else None
        customer_contract = None
        if request.contract:
            if request.contract.kind == ContractKind.CUSTOMER:
                customer_contract = request.contract
            elif request.contract.kind == ContractKind.SOLE_SUPPLIER:
                customer_contract = request.contract.parent_customer_contract

        row = _get_or_create_row(
            rows_by_key,
            article=articles_by_id[request.article_id],
            organization=request.organization,
            counterparty=request.counterparty,
            customer_contract=customer_contract,
            supplier_contract=supplier_contract,
        )
        row["requested"] = quant_money(row["requested"] + request.amount_rub)
        if request.comment.strip():
            row["_comments"].add(request.comment.strip())
        if request.approver_comment.strip():
            row["_comments"].add(request.approver_comment.strip())
        if request.payment_purpose.strip():
            row["_comments"].add(request.payment_purpose.strip())


def _accumulate_fact_rows(rows_by_key: dict, articles_by_id: dict[int, CashFlowArticle], filters: PlanFactFilters) -> None:
    query = PaymentFact.objects.filter(
        article_id__in=articles_by_id.keys(),
        date__year=filters.year,
        direction=PaymentDirection.OUTFLOW,
    ).select_related("organization", "article", "counterparty", "contract", "contract__parent_customer_contract", "currency")
    if filters.counterparty_id:
        query = query.filter(counterparty_id=filters.counterparty_id)
    if filters.organization_id:
        query = query.filter(organization_id=filters.organization_id)
    if filters.supplier_contract_id:
        query = query.filter(contract_id=filters.supplier_contract_id)
    if filters.customer_contract_id:
        query = query.filter(
            Q(contract__id=filters.customer_contract_id)
            | Q(contract__parent_customer_contract_id=filters.customer_contract_id)
        )

    for fact in query:
        supplier_contract = fact.contract if fact.contract and fact.contract.kind == ContractKind.SOLE_SUPPLIER else None
        customer_contract = None
        if fact.contract:
            if fact.contract.kind == ContractKind.CUSTOMER:
                customer_contract = fact.contract
            elif fact.contract.kind == ContractKind.SOLE_SUPPLIER:
                customer_contract = fact.contract.parent_customer_contract

        row = _get_or_create_row(
            rows_by_key,
            article=articles_by_id[fact.article_id],
            organization=fact.organization,
            counterparty=fact.counterparty,
            customer_contract=customer_contract,
            supplier_contract=supplier_contract,
        )
        rate = fact.contract.manual_exchange_rate if fact.contract_id else None
        amount_rub = _to_rub(fact.currency.code, fact.amount, rate)
        if fact.accounting_kind == AccountingKind.BU:
            row["fact_bu"] = quant_money(row["fact_bu"] + amount_rub)
        else:
            row["fact_nu"] = quant_money(row["fact_nu"] + amount_rub)
        if fact.comment.strip():
            row["_comments"].add(fact.comment.strip())


def _accumulate_reserved_rows(rows_by_key: dict, articles_by_id: dict[int, CashFlowArticle], filters: PlanFactFilters) -> None:
    contracts = Contract.objects.filter(
        kind=ContractKind.SOLE_SUPPLIER,
        date__year=filters.year,
    ).select_related("counterparty", "currency", "parent_customer_contract")
    if filters.counterparty_id:
        contracts = contracts.filter(counterparty_id=filters.counterparty_id)
    if filters.supplier_contract_id:
        contracts = contracts.filter(id=filters.supplier_contract_id)
    if filters.customer_contract_id:
        contracts = contracts.filter(parent_customer_contract_id=filters.customer_contract_id)
    contracts = list(contracts)
    if not contracts:
        return

    contract_ids = [contract.id for contract in contracts]
    bu_query = PaymentFact.objects.filter(
        contract_id__in=contract_ids,
        accounting_kind=AccountingKind.BU,
        direction=PaymentDirection.OUTFLOW,
        date__year=filters.year,
    )
    if filters.organization_id:
        bu_query = bu_query.filter(organization_id=filters.organization_id)
    latest_by_contract = {
        row["contract_id"]: row["latest_id"]
        for row in bu_query.values("contract_id").annotate(latest_id=Max("id"))
    }
    if not latest_by_contract:
        return

    latest_facts = {
        fact.contract_id: fact
        for fact in PaymentFact.objects.filter(id__in=latest_by_contract.values()).select_related("organization")
    }
    for contract in contracts:
        fact = latest_facts.get(contract.id)
        if not fact:
            continue
        article = articles_by_id.get(fact.article_id)
        if not article:
            continue
        row = _get_or_create_row(
            rows_by_key,
            article=article,
            organization=fact.organization,
            counterparty=contract.counterparty,
            customer_contract=contract.parent_customer_contract,
            supplier_contract=contract,
        )
        reserved_rub = _to_rub(contract.currency.code, contract.reserved_amount, contract.manual_exchange_rate)
        row["reserved"] = quant_money(row["reserved"] + reserved_rub)


def _get_or_create_row(
    rows_by_key: dict,
    *,
    article,
    organization,
    counterparty,
    customer_contract,
    supplier_contract,
):
    key = (
        organization.id if organization else None,
        article.id,
        counterparty.id if counterparty else None,
        customer_contract.id if customer_contract else None,
        supplier_contract.id if supplier_contract else None,
    )
    if key not in rows_by_key:
        rows_by_key[key] = _build_row(
            article=article,
            organization=organization,
            counterparty=counterparty,
            customer_contract=customer_contract,
            supplier_contract=supplier_contract,
        )
    return rows_by_key[key]


def _build_row(*, article, organization, counterparty, customer_contract, supplier_contract) -> dict:
    key = (
        organization.id if organization else None,
        article.id,
        counterparty.id if counterparty else None,
        customer_contract.id if customer_contract else None,
        supplier_contract.id if supplier_contract else None,
    )
    return {
        "_key": key,
        "_comments": set(),
        "organization": organization,
        "article": article,
        "counterparty": counterparty,
        "customer_contract": customer_contract,
        "supplier_contract": supplier_contract,
        "plan": MONEY_ZERO,
        "adjustments": MONEY_ZERO,
        "reserved": MONEY_ZERO,
        "requested": MONEY_ZERO,
        "fact_bu": MONEY_ZERO,
        "fact_nu": MONEY_ZERO,
        "balance_bu": MONEY_ZERO,
        "balance_nu": MONEY_ZERO,
        "facts_gap": MONEY_ZERO,
        "has_limit_overrun": False,
        "is_internal_turnover": False,
        "missing_in_one_c": False,
        "comments": "",
    }


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


def _to_rub(currency_code: str, amount: Decimal, manual_exchange_rate: Decimal | None) -> Decimal:
    if currency_code == RUB_CODE:
        return quant_money(amount)
    rate = manual_exchange_rate or Decimal("0")
    if rate <= 0:
        return quant_money(amount)
    return quant_money(amount * rate)


def _row_sort_key(row: dict) -> tuple:
    return (
        row["organization"].name if row["organization"] else "",
        row["counterparty"].name if row["counterparty"] else "",
        row["customer_contract"].number if row["customer_contract"] else "",
        row["supplier_contract"].number if row["supplier_contract"] else "",
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
