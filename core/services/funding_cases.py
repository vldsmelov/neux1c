"""Расчёт состояния кейса финансирования: сводка по договорам, фактам и
ожидаемым движениям. Это «карточка сделки» в стиле 1С УХ — финансист сразу
видит сколько пришло, сколько потратили, сколько ещё ждать, кому должны.
"""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from core.models import (
    ContractKind,
    FundingCase,
    PaymentDirection,
    PaymentFact,
    PaymentRequest,
    PaymentRequestStatus,
)
from core.services.payment_requests import quant_money


MONEY_ZERO = Decimal("0.00")
ACTIVE_REQUEST_STATUSES = (
    PaymentRequestStatus.PENDING_APPROVAL,
    PaymentRequestStatus.PENDING_FINAL_APPROVAL,
    PaymentRequestStatus.APPROVED,
    PaymentRequestStatus.TRANSFERRED,
)


@dataclass
class ContractStat:
    contract: object
    inflow: Decimal = MONEY_ZERO
    outflow: Decimal = MONEY_ZERO
    requested: Decimal = MONEY_ZERO  # активные заявки, ещё не оплачены
    expected_remaining: Decimal = MONEY_ZERO  # сколько ещё должны / ждём по договору


def build_case_overview(case: FundingCase) -> dict:
    """Полное состояние кейса: договоры по типам, факты, ожидаемые движения,
    рассчитанные сальдо и итог «профицит / дефицит / в плане»."""
    contracts = list(
        case.contracts.select_related("counterparty", "currency").all()
    )
    by_kind = defaultdict(list)
    contract_stats: dict[int, ContractStat] = {c.id: ContractStat(contract=c) for c in contracts}

    contract_ids = [c.id for c in contracts]

    # Все факты по договорам кейса
    facts = list(
        PaymentFact.objects.filter(contract_id__in=contract_ids)
        .select_related("article", "counterparty", "currency", "contract")
        .order_by("-date", "-id")
    )
    for fact in facts:
        stat = contract_stats[fact.contract_id]
        amount = quant_money(fact.amount * (fact.contract.manual_exchange_rate or Decimal("1")))
        if fact.direction == PaymentDirection.INFLOW:
            stat.inflow = quant_money(stat.inflow + amount)
        else:
            stat.outflow = quant_money(stat.outflow + amount)

    # Активные заявки на оплату — ещё не факт, но «в трубе»
    active_requests = list(
        PaymentRequest.objects.filter(
            contract_id__in=contract_ids,
            status__in=ACTIVE_REQUEST_STATUSES,
        ).select_related("contract", "currency", "counterparty")
    )
    for pr in active_requests:
        stat = contract_stats[pr.contract_id]
        stat.requested = quant_money(stat.requested + pr.amount_rub)

    # Группировка договоров по типам и расчёт ожидаемого остатка
    for c in contracts:
        stat = contract_stats[c.id]
        contract_total = quant_money(c.amount * (c.manual_exchange_rate or Decimal("1")))
        if c.kind == ContractKind.CUSTOMER:
            # Доходный: ожидаем приход = сумма − что уже пришло
            stat.expected_remaining = quant_money(contract_total - stat.inflow)
        elif c.kind == ContractKind.SOLE_SUPPLIER:
            # Расходный: должны заплатить = сумма − что уже ушло − что зарезервировано заявками
            stat.expected_remaining = quant_money(contract_total - stat.outflow - stat.requested)
        elif c.kind == ContractKind.LOAN_RECEIVED:
            # Заём получен: пришло − возвращено (тело + проценты)
            # остаток к возврату = принципал − что уже отдали + (примерно начисленные проценты — упрощённо берём фиксированный % от тела)
            principal_left = contract_total - stat.outflow
            interest_estimate = quant_money(contract_total * c.interest_rate / Decimal("100"))
            stat.expected_remaining = quant_money(principal_left + max(MONEY_ZERO, interest_estimate))
        elif c.kind == ContractKind.LOAN_GIVEN:
            # Заём выдан: мы должны получить обратно тело + проценты
            principal_left = contract_total - stat.inflow
            interest_estimate = quant_money(contract_total * c.interest_rate / Decimal("100"))
            stat.expected_remaining = quant_money(principal_left + max(MONEY_ZERO, interest_estimate))
        by_kind[c.kind].append(stat)

    # Итоги
    total_inflow = quant_money(sum((s.inflow for s in contract_stats.values()), MONEY_ZERO))
    total_outflow = quant_money(sum((s.outflow for s in contract_stats.values()), MONEY_ZERO))
    total_requested = quant_money(sum((s.requested for s in contract_stats.values()), MONEY_ZERO))

    # «Доступно сейчас» = всё что пришло − что ушло − что зарезервировано заявками
    available_now = quant_money(total_inflow - total_outflow - total_requested)

    # Ожидаемые приходы (доходные + выданные займы) и ожидаемые расходы
    expected_inflow_remaining = quant_money(sum(
        (s.expected_remaining for s in (*by_kind[ContractKind.CUSTOMER], *by_kind[ContractKind.LOAN_GIVEN])),
        MONEY_ZERO,
    ))
    expected_outflow_remaining = quant_money(sum(
        (s.expected_remaining for s in (*by_kind[ContractKind.SOLE_SUPPLIER], *by_kind[ContractKind.LOAN_RECEIVED])),
        MONEY_ZERO,
    ))

    # Прогнозное сальдо к закрытию кейса
    projected_balance = quant_money(available_now + expected_inflow_remaining - expected_outflow_remaining)

    if projected_balance > MONEY_ZERO:
        status_label = "профицит"
        status_class = "success"
    elif projected_balance < MONEY_ZERO:
        status_label = "дефицит"
        status_class = "danger"
    else:
        status_label = "в плане"
        status_class = "info"

    # Контрольные точки (UX-подсказки)
    alerts: list[str] = []
    supplier_obligations = quant_money(sum(
        (max(MONEY_ZERO, s.expected_remaining) for s in by_kind[ContractKind.SOLE_SUPPLIER]),
        MONEY_ZERO,
    ))
    incoming_expected = quant_money(sum(
        (max(MONEY_ZERO, s.expected_remaining) for s in by_kind[ContractKind.CUSTOMER]),
        MONEY_ZERO,
    ))
    if supplier_obligations > (available_now + incoming_expected):
        gap = quant_money(supplier_obligations - available_now - incoming_expected)
        alerts.append(
            f"Дефицит покрытия поставщиков: {gap} ₽ — нужен дополнительный источник (заём или допсоглашение к доходному договору)"
        )
    loans_to_repay = quant_money(sum(
        (max(MONEY_ZERO, s.expected_remaining) for s in by_kind[ContractKind.LOAN_RECEIVED]),
        MONEY_ZERO,
    ))
    if loans_to_repay > MONEY_ZERO:
        alerts.append(f"К возврату по займам (тело + проценты): {loans_to_repay} ₽")

    return {
        "case": case,
        "contracts_by_kind": dict(by_kind),
        "facts": facts,
        "active_requests": active_requests,
        "total_inflow": total_inflow,
        "total_outflow": total_outflow,
        "total_requested": total_requested,
        "available_now": available_now,
        "expected_inflow_remaining": expected_inflow_remaining,
        "expected_outflow_remaining": expected_outflow_remaining,
        "projected_balance": projected_balance,
        "status_label": status_label,
        "status_class": status_class,
        "alerts": alerts,
    }
