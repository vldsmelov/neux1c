from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db.models import Sum

from core.models import AccountingKind, Contract, ContractKind, PaymentFact


MONEY_QUANT = Decimal("0.01")
RUB_CODE = "RUB"


@dataclass(frozen=True)
class ContractRegisterFilters:
    customer_contract_id: int | None = None
    counterparty_id: int | None = None
    contract_query: str = ""
    date_from: date | None = None
    date_to: date | None = None

    @property
    def has_filters(self) -> bool:
        return bool(
            self.customer_contract_id
            or self.counterparty_id
            or self.contract_query
            or self.date_from
            or self.date_to
        )


def build_contract_register(filters: ContractRegisterFilters) -> tuple[list[dict], dict]:
    contracts = list(
        Contract.objects.select_related("counterparty", "currency", "parent_customer_contract")
        .prefetch_related("additional_agreements")
        .order_by("date", "number")
    )
    if not contracts:
        return [], _summary([])

    payments_by_contract = _payments_by_contract([contract.id for contract in contracts])
    suppliers_by_customer = defaultdict(list)
    customers = []
    for contract in contracts:
        if contract.kind == ContractKind.CUSTOMER:
            customers.append(contract)
        else:
            suppliers_by_customer[contract.parent_customer_contract_id].append(contract)

    rows = []
    for customer in customers:
        if filters.customer_contract_id and customer.id != filters.customer_contract_id:
            continue
        supplier_contracts = suppliers_by_customer.get(customer.id, [])
        customer_matches = _contract_matches(customer, filters, include_customer_filter=False)
        supplier_contracts = [
            supplier
            for supplier in supplier_contracts
            if customer_matches or _contract_matches(supplier, filters, include_customer_filter=False)
        ]
        if filters.has_filters and not customer_matches and not supplier_contracts:
            continue
        rows.append(_customer_row(customer, supplier_contracts, payments_by_contract))

    if not filters.customer_contract_id:
        orphan_suppliers = [
            supplier
            for supplier in suppliers_by_customer.get(None, [])
            if _contract_matches(supplier, filters, include_customer_filter=False)
        ]
        if orphan_suppliers or (not filters.has_filters and suppliers_by_customer.get(None)):
            rows.append(_orphan_row(orphan_suppliers or suppliers_by_customer.get(None, []), payments_by_contract))

    return rows, _summary(rows)


def _customer_row(customer: Contract, supplier_contracts: list[Contract], payments_by_contract: dict[int, Decimal]) -> dict:
    supplier_rows = [_supplier_row(supplier, payments_by_contract) for supplier in supplier_contracts]
    return {
        "row_type": "customer",
        "customer": customer,
        "customer_number": customer.number,
        "customer_name": customer.name,
        "customer_date": customer.date,
        "counterparty_name": customer.counterparty.name,
        "supplier_rows": supplier_rows,
        "supplier_count": len(supplier_rows),
        "agreement_count": sum(len(row["agreements"]) for row in supplier_rows),
        "reserved_rub": _sum_supplier_value(supplier_rows, "reserved_rub"),
        "paid_rub": _sum_supplier_value(supplier_rows, "paid_rub"),
        "remaining_rub": _sum_supplier_value(supplier_rows, "remaining_rub"),
    }


def _orphan_row(supplier_contracts: list[Contract], payments_by_contract: dict[int, Decimal]) -> dict:
    supplier_rows = [_supplier_row(supplier, payments_by_contract) for supplier in supplier_contracts]
    return {
        "row_type": "orphan",
        "customer": None,
        "customer_number": "Без доходного договора",
        "customer_name": "Расходные договоры без связи с доходным документом",
        "customer_date": None,
        "counterparty_name": "—",
        "supplier_rows": supplier_rows,
        "supplier_count": len(supplier_rows),
        "agreement_count": sum(len(row["agreements"]) for row in supplier_rows),
        "reserved_rub": _sum_supplier_value(supplier_rows, "reserved_rub"),
        "paid_rub": _sum_supplier_value(supplier_rows, "paid_rub"),
        "remaining_rub": _sum_supplier_value(supplier_rows, "remaining_rub"),
    }


def _supplier_row(supplier: Contract, payments_by_contract: dict[int, Decimal]) -> dict:
    reserved_native = supplier.reserved_amount
    paid_native = payments_by_contract.get(supplier.id, Decimal("0"))
    remaining_native = max(reserved_native - paid_native, Decimal("0"))
    rate = _rate_to_rub(supplier)
    agreements = list(supplier.additional_agreements.all())
    return {
        "contract": supplier,
        "agreements": agreements,
        "agreement_count": len(agreements),
        "reserved_native": _quantize(reserved_native),
        "paid_native": _quantize(paid_native),
        "remaining_native": _quantize(remaining_native),
        "reserved_rub": _quantize(reserved_native * rate),
        "paid_rub": _quantize(paid_native * rate),
        "remaining_rub": _quantize(remaining_native * rate),
    }


def _summary(rows: list[dict]) -> dict:
    return {
        "customers": sum(1 for row in rows if row["row_type"] == "customer"),
        "orphan_groups": sum(1 for row in rows if row["row_type"] == "orphan"),
        "suppliers": sum(row["supplier_count"] for row in rows),
        "agreements": sum(row["agreement_count"] for row in rows),
        "total_reserved_rub": _quantize(sum((row["reserved_rub"] for row in rows), Decimal("0"))),
        "total_paid_rub": _quantize(sum((row["paid_rub"] for row in rows), Decimal("0"))),
        "total_remaining_rub": _quantize(sum((row["remaining_rub"] for row in rows), Decimal("0"))),
    }


def _payments_by_contract(contract_ids: list[int]) -> dict[int, Decimal]:
    return {
        row["contract_id"]: row["total"] or Decimal("0")
        for row in PaymentFact.objects.filter(contract_id__in=contract_ids, accounting_kind=AccountingKind.BU)
        .values("contract_id")
        .annotate(total=Sum("amount"))
    }


def _contract_matches(contract: Contract, filters: ContractRegisterFilters, *, include_customer_filter: bool) -> bool:
    if include_customer_filter and filters.customer_contract_id and contract.id != filters.customer_contract_id:
        return False
    if filters.counterparty_id and contract.counterparty_id != filters.counterparty_id:
        return False
    if filters.contract_query:
        haystack = f"{contract.number} {contract.name} {contract.counterparty.name}".lower()
        if filters.contract_query not in haystack:
            return False
    if filters.date_from and contract.date < filters.date_from:
        return False
    if filters.date_to and contract.date > filters.date_to:
        return False
    return True


def _sum_supplier_value(supplier_rows: list[dict], key: str) -> Decimal:
    return _quantize(sum((row[key] for row in supplier_rows), Decimal("0")))


def _rate_to_rub(contract: Contract) -> Decimal:
    if contract.currency.code == RUB_CODE:
        return Decimal("1")
    return contract.manual_exchange_rate or Decimal("1")


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANT)
