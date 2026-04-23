from collections import defaultdict
from decimal import Decimal

from django.db.models import Max, Sum

from core.models import (
    AccountingKind,
    BudgetLimitPlan,
    BudgetPlanStatus,
    Contract,
    ContractKind,
    ExternalPaymentDocument,
    PaymentFact,
)


def build_contract_reservation_rows() -> list[dict]:
    contracts = list(
        Contract.objects.filter(kind=ContractKind.SOLE_SUPPLIER).select_related("counterparty", "currency")
    )
    if not contracts:
        return []

    contract_ids = [contract.id for contract in contracts]
    paid_totals = _paid_totals_by_contract(contract_ids)
    article_fact_by_contract = _latest_bu_fact_by_contract(contract_ids)
    approved_limits = _approved_limit_by_article()

    reserved_by_article = defaultdict(Decimal)
    for contract in contracts:
        article_fact = article_fact_by_contract.get(contract.id)
        if article_fact:
            reserved_by_article[article_fact.article_id] += contract.reserved_amount

    rows = []
    for contract in contracts:
        paid_amount = paid_totals.get(contract.id, Decimal("0"))
        reserved_amount = contract.reserved_amount
        remaining_amount = max(reserved_amount - paid_amount, Decimal("0"))

        article_fact = article_fact_by_contract.get(contract.id)
        article = article_fact.article if article_fact else None
        approved_limit = approved_limits.get(article.id) if article else None
        reserved_in_article = reserved_by_article.get(article.id) if article else None

        available_limit_after_reserve = None
        if approved_limit is not None and reserved_in_article is not None:
            available_limit_after_reserve = max(approved_limit - reserved_in_article, Decimal("0"))

        rows.append(
            {
                "contract": contract,
                "article": article,
                "reserved_amount": reserved_amount,
                "paid_amount": paid_amount,
                "remaining_amount": remaining_amount,
                "approved_limit": approved_limit,
                "reserved_in_article": reserved_in_article,
                "available_limit_after_reserve": available_limit_after_reserve,
            }
        )

    return rows


def build_contract_reservation_summary(rows: list[dict]) -> dict:
    total_reserved = sum((row["reserved_amount"] for row in rows), Decimal("0"))
    total_paid = sum((row["paid_amount"] for row in rows), Decimal("0"))
    total_remaining = sum((row["remaining_amount"] for row in rows), Decimal("0"))
    contracts_without_article = sum(1 for row in rows if row["article"] is None)

    return {
        "contracts": len(rows),
        "total_reserved": total_reserved,
        "total_paid": total_paid,
        "total_remaining": total_remaining,
        "contracts_without_article": contracts_without_article,
    }


def _paid_totals_by_contract(contract_ids: list[int]) -> dict[int, Decimal]:
    return {
        row["contract_id"]: row["total"] or Decimal("0")
        for row in ExternalPaymentDocument.objects.filter(contract_id__in=contract_ids)
        .values("contract_id")
        .annotate(total=Sum("amount"))
    }


def _latest_bu_fact_by_contract(contract_ids: list[int]) -> dict[int, PaymentFact]:
    latest_ids = {
        row["contract_id"]: row["latest_id"]
        for row in PaymentFact.objects.filter(contract_id__in=contract_ids, accounting_kind=AccountingKind.BU)
        .values("contract_id")
        .annotate(latest_id=Max("id"))
    }
    facts = PaymentFact.objects.filter(id__in=latest_ids.values()).select_related("article")
    return {fact.contract_id: fact for fact in facts}


def _approved_limit_by_article() -> dict[int, Decimal]:
    return {
        row["article_id"]: row["total"] or Decimal("0")
        for row in BudgetLimitPlan.objects.filter(status=BudgetPlanStatus.APPROVED)
        .values("article_id")
        .annotate(total=Sum("annual_amount"))
    }
