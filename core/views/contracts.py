"""Contracts journals: register (customer-grouped), reservations, tree."""

from django.shortcuts import render

from ..access import role_required
from ..models import Contract, ContractKind, Counterparty, UserRole
from ..services.contract_register import ContractRegisterFilters, build_contract_register
from ..services.contract_reservations import (
    build_contract_reservation_rows,
    build_contract_reservation_summary,
)
from ..services.contract_tree import ContractTreeFilters, build_contract_tree
from ._shared import parse_optional_date, parse_optional_int


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def contracts_register(request):
    filters = ContractRegisterFilters(
        customer_contract_id=parse_optional_int(request.GET.get("customer_contract_id")),
        counterparty_id=parse_optional_int(request.GET.get("counterparty_id")),
        contract_query=(request.GET.get("contract") or "").strip().lower(),
        date_from=parse_optional_date(request.GET.get("date_from")),
        date_to=parse_optional_date(request.GET.get("date_to")),
    )
    rows, summary = build_contract_register(filters)
    counterparties = Counterparty.objects.filter(contracts__isnull=False).distinct().order_by("name")
    customer_contracts = (
        Contract.objects.filter(kind=ContractKind.CUSTOMER)
        .select_related("counterparty")
        .order_by("date", "number")
    )
    return render(
        request,
        "core/contracts_register.html",
        {
            "active_section": "contracts",
            "rows": rows,
            "summary": summary,
            "counterparties": counterparties,
            "customer_contracts": customer_contracts,
            "selected_customer_contract_id": filters.customer_contract_id,
            "selected_counterparty_id": filters.counterparty_id,
            "selected_contract": request.GET.get("contract", ""),
            "selected_date_from": request.GET.get("date_from", ""),
            "selected_date_to": request.GET.get("date_to", ""),
        },
    )


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def contracts_reservations(request):
    rows = build_contract_reservation_rows()
    summary = build_contract_reservation_summary(rows)
    return render(
        request,
        "core/contracts_reservations.html",
        {
            "active_section": "contracts",
            "rows": rows,
            "summary": summary,
        },
    )


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def contracts_tree(request):
    filters = ContractTreeFilters(
        counterparty_id=parse_optional_int(request.GET.get("counterparty_id")),
        contract_query=(request.GET.get("contract") or "").strip().lower(),
        date_from=parse_optional_date(request.GET.get("date_from")),
        date_to=parse_optional_date(request.GET.get("date_to")),
    )
    rows, summary = build_contract_tree(filters)
    counterparties = Counterparty.objects.filter(contracts__isnull=False).distinct().order_by("name")
    return render(
        request,
        "core/contracts_tree.html",
        {
            "active_section": "contracts",
            "rows": rows,
            "summary": summary,
            "counterparties": counterparties,
            "selected_counterparty_id": filters.counterparty_id,
            "selected_contract": request.GET.get("contract", ""),
            "selected_date_from": request.GET.get("date_from", ""),
            "selected_date_to": request.GET.get("date_to", ""),
        },
    )
