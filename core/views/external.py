"""External accounting (mock 1С:БП) views — used by the accountant role.

Эти view изолированы от основного контура: их разрешает ``external_accounting_required``
(см. ``core.access``), и зависят они только от модели договоров и сервисов
внешних оплат.
"""

from decimal import InvalidOperation

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from ..access import external_accounting_required
from ..models import Contract, ContractKind
from ..services.external_accounting import (
    paid_amount_for_contract,
    pay_contract,
    remaining_contract_amount,
)
from ._shared import parse_decimal


@external_accounting_required
def external_accounting(request):
    if request.method == "POST":
        contract = get_object_or_404(
            Contract,
            pk=request.POST.get("contract_id"),
            kind=ContractKind.SOLE_SUPPLIER,
        )
        try:
            amount = parse_decimal(request.POST.get("amount")) or remaining_contract_amount(contract)
            payment = pay_contract(contract=contract, accountant=request.user, amount=amount)
            messages.success(request, f"Оплата {payment.number} проведена")
        except (InvalidOperation, ValueError) as exc:
            messages.error(request, str(exc))
        return redirect("external_accounting")

    supplier_contracts = Contract.objects.filter(kind=ContractKind.SOLE_SUPPLIER).select_related(
        "counterparty",
        "currency",
    )
    rows = []
    for contract in supplier_contracts:
        paid_amount = paid_amount_for_contract(contract)
        remaining_amount = remaining_contract_amount(contract)
        rows.append(
            {
                "contract": contract,
                "reserved_amount": contract.reserved_amount,
                "paid_amount": paid_amount,
                "remaining_amount": remaining_amount,
            }
        )

    return render(
        request,
        "core/external_accounting.html",
        {
            "active_section": "external_accounting",
            "rows": rows,
            "payments_total": sum(row["paid_amount"] for row in rows),
        },
    )


@external_accounting_required
def external_payment_wizard(request):
    submitted: dict = {}
    if request.method == "POST":
        contract = get_object_or_404(
            Contract,
            pk=request.POST.get("contract_id"),
            kind=ContractKind.SOLE_SUPPLIER,
        )
        try:
            amount = parse_decimal(request.POST.get("amount")) or remaining_contract_amount(contract)
            payment = pay_contract(contract=contract, accountant=request.user, amount=amount)
            messages.success(request, f"Оплата {payment.number} проведена")
            return redirect("external_accounting")
        except (InvalidOperation, ValueError) as exc:
            messages.error(request, str(exc))
            submitted = request.POST

    supplier_contracts = Contract.objects.filter(kind=ContractKind.SOLE_SUPPLIER).select_related(
        "counterparty",
        "currency",
    )
    rows = []
    for contract in supplier_contracts:
        paid_amount = paid_amount_for_contract(contract)
        remaining_amount = remaining_contract_amount(contract)
        if remaining_amount > 0:
            rows.append(
                {
                    "contract": contract,
                    "paid_amount": paid_amount,
                    "remaining_amount": remaining_amount,
                }
            )
    return render(
        request,
        "core/external_payment_wizard.html",
        {
            "active_section": "external_accounting",
            "rows": rows,
            "submitted": submitted,
        },
    )
