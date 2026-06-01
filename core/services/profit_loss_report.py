"""БДР (бюджет доходов и расходов) — P&L отчёт по методу начисления.

В отличие от БДДС (cash flow), БДР смотрит на доходы и расходы в момент
их возникновения, а не в момент движения денег:
* CUSTOMER договор подписан на 2M → 2M дохода начислено
* SUPPLIER договор подписан на 2.5M → 2.5M расхода начислено
* LOAN_RECEIVED не идёт в БДР (это обязательство, не доход)
* Прибыль = доходы − расходы по начислению

Дополнительно показываем cash-эквиваленты из PaymentFact, чтобы видеть
разрыв между «начислено» и «получено/уплачено» — классическая дельта
кассового метода vs метода начисления.
"""

from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Sum

from core.models import (
    Contract,
    ContractKind,
    PaymentDirection,
    PaymentFact,
)


MONEY_ZERO = Decimal("0.00")


@dataclass(frozen=True)
class ProfitLossFilters:
    year: int
    organization_id: int | None = None
    funding_case_id: int | None = None


def build_profit_loss(filters: ProfitLossFilters) -> dict:
    """Считает БДР за год: доходы / расходы / прибыль по начислению,
    плюс cash-эквиваленты и дельту между ними."""

    # Доходы начислены: CUSTOMER контракты, подписанные в этом году
    income_q = Contract.objects.filter(
        kind=ContractKind.CUSTOMER,
        date__year=filters.year,
    )
    expense_q = Contract.objects.filter(
        kind=ContractKind.SOLE_SUPPLIER,
        date__year=filters.year,
    )
    if filters.organization_id:
        # У договоров нет напрямую organization — связь через counterparty к
        # организации в нашем случае не строится. Используем organization из
        # PaymentFact как прокси: если хотя бы один факт по этому договору
        # принадлежит этой организации.
        cust_ids = set(
            PaymentFact.objects
            .filter(contract__kind=ContractKind.CUSTOMER, organization_id=filters.organization_id)
            .values_list("contract_id", flat=True)
        )
        sup_ids = set(
            PaymentFact.objects
            .filter(contract__kind=ContractKind.SOLE_SUPPLIER, organization_id=filters.organization_id)
            .values_list("contract_id", flat=True)
        )
        income_q = income_q.filter(id__in=cust_ids)
        expense_q = expense_q.filter(id__in=sup_ids)
    if filters.funding_case_id:
        income_q = income_q.filter(funding_case_id=filters.funding_case_id)
        expense_q = expense_q.filter(funding_case_id=filters.funding_case_id)

    income_total = income_q.aggregate(t=Sum("amount"))["t"] or MONEY_ZERO
    expense_total = expense_q.aggregate(t=Sum("amount"))["t"] or MONEY_ZERO
    profit_accrued = income_total - expense_total

    # Cash-эквиваленты из PaymentFact
    fact_q = PaymentFact.objects.filter(date__year=filters.year)
    if filters.organization_id:
        fact_q = fact_q.filter(organization_id=filters.organization_id)
    if filters.funding_case_id:
        fact_q = fact_q.filter(contract__funding_case_id=filters.funding_case_id)
    cash_in = fact_q.filter(direction=PaymentDirection.INFLOW).aggregate(t=Sum("amount"))["t"] or MONEY_ZERO
    cash_out = fact_q.filter(direction=PaymentDirection.OUTFLOW).aggregate(t=Sum("amount"))["t"] or MONEY_ZERO
    cash_net = cash_in - cash_out

    # Дельта между начислением и кассой (классический accrual vs cash)
    income_gap = income_total - cash_in   # «начислено больше, чем получено» — дебиторка
    expense_gap = expense_total - cash_out  # «начислено больше, чем уплачено» — кредиторка

    # Топ доходных и расходных позиций
    income_rows = list(
        income_q.select_related("counterparty", "currency")
        .order_by("-amount")[:10]
    )
    expense_rows = list(
        expense_q.select_related("counterparty", "currency")
        .order_by("-amount")[:10]
    )

    margin_pct = (profit_accrued / income_total * 100) if income_total else Decimal("0")

    return {
        "filters": filters,
        "income_total": income_total,
        "expense_total": expense_total,
        "profit_accrued": profit_accrued,
        "margin_pct": margin_pct.quantize(Decimal("0.1")),
        "income_rows": income_rows,
        "expense_rows": expense_rows,
        "cash_in": cash_in,
        "cash_out": cash_out,
        "cash_net": cash_net,
        "income_gap": income_gap,
        "expense_gap": expense_gap,
        "income_contracts_count": income_q.count(),
        "expense_contracts_count": expense_q.count(),
    }
