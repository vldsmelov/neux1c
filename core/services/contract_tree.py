from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db.models import Sum

from core.models import AccountingKind, AdditionalAgreement, Contract, ContractKind, PaymentFact


MONEY_QUANT = Decimal("0.01")
RUB_CODE = "RUB"


@dataclass(frozen=True)
class ContractTreeFilters:
    counterparty_id: int | None = None
    contract_query: str = ""
    date_from: date | None = None
    date_to: date | None = None

    @property
    def has_filters(self) -> bool:
        return bool(self.counterparty_id or self.contract_query or self.date_from or self.date_to)


@dataclass(frozen=True)
class ContractMetrics:
    reserved_native: Decimal
    paid_native: Decimal
    remaining_native: Decimal
    reserved_rub: Decimal
    paid_rub: Decimal
    remaining_rub: Decimal


def build_contract_tree(filters: ContractTreeFilters) -> tuple[list[dict], dict]:
    contracts = list(
        Contract.objects.select_related("counterparty", "currency", "parent_customer_contract")
        .prefetch_related("additional_agreements")
        .order_by("date", "number")
    )
    if not contracts:
        return [], _summary([], [], [])

    payments_by_contract = _payments_by_contract([contract.id for contract in contracts])
    suppliers_by_parent = defaultdict(list)
    customers = []
    for contract in contracts:
        if contract.kind == ContractKind.CUSTOMER:
            customers.append(contract)
        else:
            suppliers_by_parent[contract.parent_customer_contract_id].append(contract)

    matched_contract_ids = {
        contract.id for contract in contracts if _contract_matches(contract, filters)
    }
    rows = []
    customer_rows = []
    supplier_rows = []
    agreement_rows = []

    for customer in customers:
        supplier_contracts = suppliers_by_parent.get(customer.id, [])
        if filters.has_filters:
            supplier_contracts = [contract for contract in supplier_contracts if contract.id in matched_contract_ids]
            if customer.id not in matched_contract_ids and not supplier_contracts:
                continue

        customer_metrics = _aggregate_supplier_metrics(supplier_contracts, payments_by_contract)
        row = _build_customer_row(customer, customer_metrics)
        rows.append(row)
        customer_rows.append(row)

        supplier_data_rows, agreement_data_rows = _build_supplier_rows(
            supplier_contracts,
            payments_by_contract,
            level=1,
        )
        rows.extend(supplier_data_rows)
        rows.extend(agreement_data_rows)
        supplier_rows.extend(supplier_data_rows)
        agreement_rows.extend(agreement_data_rows)

    orphan_suppliers = suppliers_by_parent.get(None, [])
    if filters.has_filters:
        orphan_suppliers = [contract for contract in orphan_suppliers if contract.id in matched_contract_ids]
    if orphan_suppliers:
        orphan_metrics = _aggregate_supplier_metrics(orphan_suppliers, payments_by_contract)
        orphan_row = {
            "row_type": "group",
            "level": 0,
            "indent_px": 0,
            "kind_label": "ГРУППА",
            "display_name": "Без доходного договора",
            "date": None,
            "counterparty_name": "—",
            "currency_code": RUB_CODE,
            "reserved_native": None,
            "paid_native": None,
            "remaining_native": None,
            "reserved_rub": orphan_metrics.reserved_rub,
            "paid_rub": orphan_metrics.paid_rub,
            "remaining_rub": orphan_metrics.remaining_rub,
        }
        rows.append(orphan_row)
        customer_rows.append(orphan_row)

        supplier_data_rows, agreement_data_rows = _build_supplier_rows(
            orphan_suppliers,
            payments_by_contract,
            level=1,
        )
        rows.extend(supplier_data_rows)
        rows.extend(agreement_data_rows)
        supplier_rows.extend(supplier_data_rows)
        agreement_rows.extend(agreement_data_rows)

    summary = _summary(customer_rows, supplier_rows, agreement_rows)
    return rows, summary


def _build_supplier_rows(
    supplier_contracts: list[Contract],
    payments_by_contract: dict[int, Decimal],
    *,
    level: int,
) -> tuple[list[dict], list[dict]]:
    supplier_rows = []
    agreement_rows = []

    for supplier in supplier_contracts:
        metrics = _contract_metrics(supplier, payments_by_contract)
        supplier_rows.append(
            {
                "row_type": "supplier",
                "level": level,
                "indent_px": level * 22,
                "kind_label": "ПОСТАВЩИК",
                "display_name": f"{supplier.number} · {supplier.name}",
                "date": supplier.date,
                "counterparty_name": supplier.counterparty.name,
                "currency_code": supplier.currency.code,
                "reserved_native": metrics.reserved_native,
                "paid_native": metrics.paid_native,
                "remaining_native": metrics.remaining_native,
                "reserved_rub": metrics.reserved_rub,
                "paid_rub": metrics.paid_rub,
                "remaining_rub": metrics.remaining_rub,
            }
        )
        agreement_rows.extend(_build_agreement_rows(supplier, level=level + 1))

    return supplier_rows, agreement_rows


def _build_agreement_rows(supplier: Contract, *, level: int) -> list[dict]:
    rows = []
    contract_rate = _rate_to_rub(supplier)
    for agreement in supplier.additional_agreements.all():
        metrics = _agreement_metrics(agreement, contract_rate)
        rows.append(
            {
                "row_type": "agreement",
                "level": level,
                "indent_px": level * 22,
                "kind_label": "ДС",
                "display_name": f"{agreement.number} · {agreement.contract.number}",
                "date": agreement.date,
                "counterparty_name": supplier.counterparty.name,
                "currency_code": agreement.currency.code,
                "reserved_native": metrics.reserved_native,
                "paid_native": metrics.paid_native,
                "remaining_native": metrics.remaining_native,
                "reserved_rub": metrics.reserved_rub,
                "paid_rub": metrics.paid_rub,
                "remaining_rub": metrics.remaining_rub,
            }
        )
    return rows


def _build_customer_row(customer: Contract, metrics: ContractMetrics) -> dict:
    return {
        "row_type": "customer",
        "level": 0,
        "indent_px": 0,
        "kind_label": "ДОХОДНЫЙ",
        "display_name": f"{customer.number} · {customer.name}",
        "date": customer.date,
        "counterparty_name": customer.counterparty.name,
        "currency_code": RUB_CODE,
        "reserved_native": None,
        "paid_native": None,
        "remaining_native": None,
        "reserved_rub": metrics.reserved_rub,
        "paid_rub": metrics.paid_rub,
        "remaining_rub": metrics.remaining_rub,
    }


def _summary(customer_rows: list[dict], supplier_rows: list[dict], agreement_rows: list[dict]) -> dict:
    return {
        "customers": len(customer_rows),
        "suppliers": len(supplier_rows),
        "agreements": len(agreement_rows),
        "total_reserved_rub": _quantize(sum((row["reserved_rub"] for row in supplier_rows), Decimal("0"))),
        "total_paid_rub": _quantize(sum((row["paid_rub"] for row in supplier_rows), Decimal("0"))),
        "total_remaining_rub": _quantize(sum((row["remaining_rub"] for row in supplier_rows), Decimal("0"))),
    }


def _payments_by_contract(contract_ids: list[int]) -> dict[int, Decimal]:
    return {
        row["contract_id"]: row["total"] or Decimal("0")
        for row in PaymentFact.objects.filter(contract_id__in=contract_ids, accounting_kind=AccountingKind.BU)
        .values("contract_id")
        .annotate(total=Sum("amount"))
    }


def _aggregate_supplier_metrics(
    supplier_contracts: list[Contract],
    payments_by_contract: dict[int, Decimal],
) -> ContractMetrics:
    total_reserved_rub = Decimal("0")
    total_paid_rub = Decimal("0")
    total_remaining_rub = Decimal("0")
    for supplier in supplier_contracts:
        metrics = _contract_metrics(supplier, payments_by_contract)
        total_reserved_rub += metrics.reserved_rub
        total_paid_rub += metrics.paid_rub
        total_remaining_rub += metrics.remaining_rub
    return ContractMetrics(
        reserved_native=Decimal("0"),
        paid_native=Decimal("0"),
        remaining_native=Decimal("0"),
        reserved_rub=_quantize(total_reserved_rub),
        paid_rub=_quantize(total_paid_rub),
        remaining_rub=_quantize(total_remaining_rub),
    )


def _contract_metrics(contract: Contract, payments_by_contract: dict[int, Decimal]) -> ContractMetrics:
    reserved_native = contract.reserved_amount
    paid_native = payments_by_contract.get(contract.id, Decimal("0"))
    remaining_native = max(reserved_native - paid_native, Decimal("0"))
    rate = _rate_to_rub(contract)
    return ContractMetrics(
        reserved_native=_quantize(reserved_native),
        paid_native=_quantize(paid_native),
        remaining_native=_quantize(remaining_native),
        reserved_rub=_quantize(reserved_native * rate),
        paid_rub=_quantize(paid_native * rate),
        remaining_rub=_quantize(remaining_native * rate),
    )


def _agreement_metrics(agreement: AdditionalAgreement, rate_to_rub: Decimal) -> ContractMetrics:
    reserved_native = agreement.amount
    paid_native = Decimal("0")
    remaining_native = agreement.amount
    return ContractMetrics(
        reserved_native=_quantize(reserved_native),
        paid_native=_quantize(paid_native),
        remaining_native=_quantize(remaining_native),
        reserved_rub=_quantize(reserved_native * rate_to_rub),
        paid_rub=_quantize(paid_native),
        remaining_rub=_quantize(remaining_native * rate_to_rub),
    )


def _rate_to_rub(contract: Contract) -> Decimal:
    if contract.currency.code == RUB_CODE:
        return Decimal("1")
    return contract.manual_exchange_rate or Decimal("1")


def _contract_matches(contract: Contract, filters: ContractTreeFilters) -> bool:
    if filters.counterparty_id and contract.counterparty_id != filters.counterparty_id:
        return False
    if filters.contract_query:
        haystack = f"{contract.number} {contract.name}".lower()
        if filters.contract_query not in haystack:
            return False
    if filters.date_from and contract.date < filters.date_from:
        return False
    if filters.date_to and contract.date > filters.date_to:
        return False
    return True


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANT)
