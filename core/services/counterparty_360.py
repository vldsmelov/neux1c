"""Counterparty 360 — полное досье контрагента: договоры, движения денег,
участие в кейсах, сальдо. То что финансисты собирают руками из 5 разных
журналов — на одной странице.
"""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from core.models import (
    Contract,
    ContractKind,
    Counterparty,
    FundingCase,
    PaymentDirection,
    PaymentFact,
    PaymentRequest,
    PaymentRequestStatus,
)


MONEY_ZERO = Decimal("0.00")


@dataclass
class ContractSummary:
    contract: Contract
    inflow: Decimal = MONEY_ZERO
    outflow: Decimal = MONEY_ZERO
    requested: Decimal = MONEY_ZERO
    outstanding: Decimal = MONEY_ZERO  # сколько ещё должны / ждём


def build_counterparty_overview(counterparty: Counterparty) -> dict:
    """Полное досье контрагента: договоры по типам, факты, кейсы,
    сальдо «они нам / мы им»."""
    contracts = list(
        Contract.objects.filter(counterparty=counterparty)
        .select_related("currency", "funding_case")
        .order_by("-date")
    )
    contract_summaries: dict[int, ContractSummary] = {c.id: ContractSummary(contract=c) for c in contracts}
    contract_ids = [c.id for c in contracts]

    # Лайфтайм-движения по этим контрактам
    facts = (
        PaymentFact.objects.filter(contract_id__in=contract_ids)
        .select_related("article", "contract", "currency", "organization")
        .order_by("-date")
    )
    lifetime_inflow = MONEY_ZERO
    lifetime_outflow = MONEY_ZERO
    for fact in facts:
        cs = contract_summaries.get(fact.contract_id)
        if not cs:
            continue
        amount = Decimal(fact.amount)
        if fact.direction == PaymentDirection.INFLOW:
            cs.inflow += amount
            lifetime_inflow += amount
        else:
            cs.outflow += amount
            lifetime_outflow += amount

    # Активные заявки на оплату
    active_requests = PaymentRequest.objects.filter(
        contract_id__in=contract_ids,
        status__in=[
            PaymentRequestStatus.PENDING_APPROVAL,
            PaymentRequestStatus.PENDING_FINAL_APPROVAL,
            PaymentRequestStatus.APPROVED,
            PaymentRequestStatus.TRANSFERRED,
        ],
    ).select_related("contract", "currency", "approver", "author")
    requested_total = MONEY_ZERO
    for pr in active_requests:
        cs = contract_summaries.get(pr.contract_id)
        if not cs:
            continue
        cs.requested += pr.amount_rub
        requested_total += pr.amount_rub

    # Outstanding по каждому контракту: для CUSTOMER они нам должны = amount - inflow;
    # для SUPPLIER мы им должны = amount - outflow - requested.
    by_kind = defaultdict(list)
    they_owe_us = MONEY_ZERO  # CUSTOMER outstanding + LOAN_GIVEN
    we_owe_them = MONEY_ZERO  # SUPPLIER outstanding + LOAN_RECEIVED
    for cs in contract_summaries.values():
        c = cs.contract
        if c.kind == ContractKind.CUSTOMER:
            cs.outstanding = max(MONEY_ZERO, c.amount - cs.inflow)
            they_owe_us += cs.outstanding
        elif c.kind == ContractKind.SOLE_SUPPLIER:
            cs.outstanding = max(MONEY_ZERO, c.amount - cs.outflow - cs.requested)
            we_owe_them += cs.outstanding
        elif c.kind == ContractKind.LOAN_RECEIVED:
            # тело + проценты к возврату
            interest = (c.amount * (c.interest_rate or 0) / Decimal("100"))
            cs.outstanding = max(MONEY_ZERO, c.amount - cs.outflow + interest)
            we_owe_them += cs.outstanding
        elif c.kind == ContractKind.LOAN_GIVEN:
            interest = (c.amount * (c.interest_rate or 0) / Decimal("100"))
            cs.outstanding = max(MONEY_ZERO, c.amount - cs.inflow + interest)
            they_owe_us += cs.outstanding
        by_kind[c.kind].append(cs)

    # Кейсы финансирования в которых участвует этот контрагент
    related_cases = (
        FundingCase.objects
        .filter(contracts__counterparty=counterparty)
        .distinct()
        .select_related("organization", "owner")
        .order_by("-opened_at")
    )

    # Топ-10 свежих фактов и активных заявок
    recent_facts = list(facts[:10])
    recent_requests = list(active_requests.order_by("-request_date")[:10])

    net = they_owe_us - we_owe_them
    if net > 0:
        net_label = "профицит (они нам)"
        net_tone = "success"
    elif net < 0:
        net_label = "дефицит (мы им)"
        net_tone = "danger"
    else:
        net_label = "сальдо ноль"
        net_tone = "info"

    return {
        "counterparty": counterparty,
        "contracts_count": len(contracts),
        "contracts_by_kind": dict(by_kind),
        "lifetime_inflow": lifetime_inflow,
        "lifetime_outflow": lifetime_outflow,
        "lifetime_net": lifetime_inflow - lifetime_outflow,
        "requested_total": requested_total,
        "they_owe_us": they_owe_us,
        "we_owe_them": we_owe_them,
        "net": net,
        "net_label": net_label,
        "net_tone": net_tone,
        "related_cases": list(related_cases),
        "recent_facts": recent_facts,
        "recent_requests": recent_requests,
    }
