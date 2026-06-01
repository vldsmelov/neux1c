"""Расчёт состояния кейса финансирования: сводка по договорам, фактам и
ожидаемым движениям. Это «карточка сделки» в стиле 1С УХ — финансист сразу
видит сколько пришло, сколько потратили, сколько ещё ждать, кому должны.
"""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from django.db import models

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


def _contract_icon(kind: str) -> str:
    return {
        "customer": "📥",
        "sole_supplier": "📤",
        "loan_received": "💰",
        "loan_given": "💸",
    }.get(kind, "📄")


def _contract_kind_label(kind: str) -> str:
    return {
        "customer": "Доходный договор",
        "sole_supplier": "Договор с поставщиком",
        "loan_received": "Заём получен",
        "loan_given": "Заём выдан",
    }.get(kind, "Договор")


def _build_sankey(contracts, facts, contract_stats, case) -> dict:
    """Готовит SVG-ready данные для Sankey-диаграммы движений денег.

    Схема: левая колонка — источники (CUSTOMER inflows + LOAN_RECEIVED inflows),
    центральная нода — сам кейс, правая колонка — получатели (SUPPLIER
    outflows + возвраты по LOAN_RECEIVED). Ширина ленты ∝ сумме.
    """
    inflow_by_source: dict[int, dict] = {}  # contract_id -> {label, amount}
    outflow_by_sink: dict[int, dict] = {}

    for fact in facts:
        amount = Decimal(fact.amount)
        if fact.direction == PaymentDirection.INFLOW and fact.contract_id in contract_stats:
            key = fact.contract_id
            bucket = inflow_by_source.setdefault(key, {
                "id": key,
                "label": fact.contract.counterparty.name if fact.contract else "—",
                "subtitle": fact.contract.number if fact.contract else "",
                "amount": MONEY_ZERO,
                "kind": fact.contract.kind if fact.contract else "",
            })
            bucket["amount"] = quant_money(bucket["amount"] + amount)
        elif fact.direction == PaymentDirection.OUTFLOW:
            # Возврат займа группируется по loan_repayment_contract, иначе — по contract
            target_id = fact.loan_repayment_contract_id or fact.contract_id
            if target_id not in contract_stats:
                continue
            target = fact.loan_repayment_contract if fact.loan_repayment_contract_id else fact.contract
            key = target_id
            bucket = outflow_by_sink.setdefault(key, {
                "id": key,
                "label": target.counterparty.name,
                "subtitle": target.number,
                "amount": MONEY_ZERO,
                "kind": target.kind,
            })
            bucket["amount"] = quant_money(bucket["amount"] + amount)

    sources = sorted(inflow_by_source.values(), key=lambda b: -b["amount"])
    sinks = sorted(outflow_by_sink.values(), key=lambda b: -b["amount"])
    if not sources and not sinks:
        return {"empty": True}

    # Подбор пропорций для SVG — нормируем ширины колонок и баров на максимум
    total = max(
        sum((b["amount"] for b in sources), MONEY_ZERO),
        sum((b["amount"] for b in sinks), MONEY_ZERO),
        Decimal("1"),
    )

    # Базовая геометрия SVG: 800×400 (CSS можно растянуть)
    canvas_w = 800
    canvas_h = 400
    col_w = 180
    middle_w = 140
    gap_x = (canvas_w - 2 * col_w - middle_w) / 2  # gap between cols

    def _y_positions(nodes, top_pad=20, bottom_pad=20):
        """Вертикальные y-позиции для нод, размером пропорциональным сумме."""
        if not nodes:
            return []
        available = canvas_h - top_pad - bottom_pad
        total_amount = sum((n["amount"] for n in nodes), MONEY_ZERO) or Decimal("1")
        gap = 12
        # Учитываем gaps между нодами
        usable = available - gap * (len(nodes) - 1)
        positions = []
        y = top_pad
        for n in nodes:
            h = max(28, int(usable * float(n["amount"]) / float(total_amount)))
            positions.append({"node": n, "y": y, "h": h})
            y += h + gap
        return positions

    src_positions = _y_positions(sources)
    snk_positions = _y_positions(sinks)

    # Полные ленты: каждая нода связывается с центральным хабом
    src_x = 0
    middle_x = col_w + gap_x
    middle_h = sum(p["h"] for p in src_positions) or sum(p["h"] for p in snk_positions) or 60
    middle_y = int((canvas_h - middle_h) / 2)
    sink_x = int(middle_x + middle_w + gap_x)
    middle_x = int(middle_x)

    flows = []
    for p in src_positions:
        flows.append({
            "from_x": src_x + col_w,
            "from_y": int(p["y"] + p["h"] / 2),
            "to_x": middle_x,
            "to_y": int(middle_y + middle_h / 2),
            "width": p["h"],
            "color_kind": p["node"]["kind"],
        })
    for p in snk_positions:
        flows.append({
            "from_x": middle_x + middle_w,
            "from_y": int(middle_y + middle_h / 2),
            "to_x": sink_x,
            "to_y": int(p["y"] + p["h"] / 2),
            "width": p["h"],
            "color_kind": p["node"]["kind"],
        })

    return {
        "empty": False,
        "canvas_w": canvas_w,
        "canvas_h": canvas_h,
        "src_col_x": src_x,
        "src_col_w": col_w,
        "src_positions": src_positions,
        "snk_col_x": sink_x,
        "snk_col_w": col_w,
        "snk_positions": snk_positions,
        "middle_x": middle_x,
        "middle_y": middle_y,
        "middle_w": middle_w,
        "middle_h": middle_h,
        "middle_label": case.code,
        "flows": flows,
        "total": quant_money(total),
    }


def _contract_tone(kind: str) -> str:
    return {
        "customer": "success",
        "sole_supplier": "danger",
        "loan_received": "warning",
        "loan_given": "info",
    }.get(kind, "info")
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
    # Поля для займов:
    principal_repaid: Decimal = MONEY_ZERO
    interest_paid: Decimal = MONEY_ZERO
    interest_estimate: Decimal = MONEY_ZERO
    schedule: list = None


def build_case_overview(case: FundingCase) -> dict:
    """Полное состояние кейса: договоры по типам, факты, ожидаемые движения,
    рассчитанные сальдо и итог «профицит / дефицит / в плане»."""
    contracts = list(
        case.contracts.select_related("counterparty", "currency").all()
    )
    by_kind = defaultdict(list)
    contract_stats: dict[int, ContractStat] = {c.id: ContractStat(contract=c) for c in contracts}

    contract_ids = [c.id for c in contracts]

    # Все факты по договорам кейса — собственным + те, что гасят займы кейса
    facts = list(
        PaymentFact.objects.filter(
            models.Q(contract_id__in=contract_ids)
            | models.Q(loan_repayment_contract_id__in=contract_ids)
        )
        .select_related("article", "counterparty", "currency", "contract", "loan_repayment_contract")
        .order_by("-date", "-id")
    )
    loan_repayment_totals: dict[int, Decimal] = defaultdict(lambda: MONEY_ZERO)
    loan_interest_totals: dict[int, Decimal] = defaultdict(lambda: MONEY_ZERO)
    for fact in facts:
        # 1. факт привязан к договору кейса → классический inflow/outflow на этот договор
        if fact.contract_id in contract_stats:
            stat = contract_stats[fact.contract_id]
            rate = (fact.contract.manual_exchange_rate or Decimal("1")) if fact.contract else Decimal("1")
            amount = quant_money(fact.amount * rate)
            if fact.direction == PaymentDirection.INFLOW:
                stat.inflow = quant_money(stat.inflow + amount)
            else:
                stat.outflow = quant_money(stat.outflow + amount)
        # 2. факт-погашение займа (целевой платёж по конкретному займу кейса)
        if fact.loan_repayment_contract_id in contract_stats:
            principal_part = quant_money(fact.amount - (fact.loan_interest_portion or MONEY_ZERO))
            loan_repayment_totals[fact.loan_repayment_contract_id] = quant_money(
                loan_repayment_totals[fact.loan_repayment_contract_id] + principal_part
            )
            loan_interest_totals[fact.loan_repayment_contract_id] = quant_money(
                loan_interest_totals[fact.loan_repayment_contract_id] + (fact.loan_interest_portion or MONEY_ZERO)
            )

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
            # Заём получен: тело должны вернуть − что уже отдали по телу;
            # проценты — оценочные минус что уже выплатили процентами.
            principal_repaid = loan_repayment_totals.get(c.id, MONEY_ZERO)
            interest_paid = loan_interest_totals.get(c.id, MONEY_ZERO)
            interest_estimate = quant_money(contract_total * c.interest_rate / Decimal("100"))
            principal_left = max(MONEY_ZERO, contract_total - principal_repaid)
            interest_left = max(MONEY_ZERO, interest_estimate - interest_paid)
            stat.expected_remaining = quant_money(principal_left + interest_left)
            stat.principal_repaid = principal_repaid
            stat.interest_paid = interest_paid
            stat.interest_estimate = interest_estimate
            # Подтянуть строки графика, если есть
            stat.schedule = list(c.schedule_lines.all().order_by("period"))
        elif c.kind == ContractKind.LOAN_GIVEN:
            # Заём выдан: мы должны получить обратно тело + проценты
            principal_left = max(MONEY_ZERO, contract_total - stat.inflow)
            interest_estimate = quant_money(contract_total * c.interest_rate / Decimal("100"))
            stat.expected_remaining = quant_money(principal_left + interest_estimate)
            stat.interest_estimate = interest_estimate
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
    if supplier_obligations > MONEY_ZERO and supplier_obligations > (available_now + incoming_expected):
        gap = quant_money(supplier_obligations - available_now - incoming_expected)
        alerts.append(
            f"Дефицит покрытия поставщиков: {gap} ₽ — нужен дополнительный источник (заём или допсоглашение к доходному договору)"
        )
    # Кейс ушёл в кассовый минус (потратили больше чем имеем + ожидаем)
    if projected_balance < MONEY_ZERO and supplier_obligations <= MONEY_ZERO:
        alerts.append(
            f"Кассовый разрыв: проект ушёл в минус на {abs(projected_balance)} ₽ — нужно поступление от доходных договоров или новый источник финансирования"
        )
    loans_to_repay = quant_money(sum(
        (max(MONEY_ZERO, s.expected_remaining) for s in by_kind[ContractKind.LOAN_RECEIVED]),
        MONEY_ZERO,
    ))
    if loans_to_repay > MONEY_ZERO:
        alerts.append(f"К возврату по займам (тело + проценты): {loans_to_repay} ₽")

    # Сальдо по контрагентам внутри кейса: «кто кому сколько должен»
    saldo_by_cp: dict[int, dict] = {}
    for c in contracts:
        cp = c.counterparty
        stat = contract_stats[c.id]
        bucket = saldo_by_cp.setdefault(cp.id, {
            "counterparty": cp,
            "inflow": MONEY_ZERO,
            "outflow": MONEY_ZERO,
            "owed_to_us": MONEY_ZERO,    # они должны нам
            "we_owe": MONEY_ZERO,        # мы должны им
        })
        bucket["inflow"] = quant_money(bucket["inflow"] + stat.inflow)
        bucket["outflow"] = quant_money(bucket["outflow"] + stat.outflow)
        if c.kind in (ContractKind.CUSTOMER, ContractKind.LOAN_GIVEN):
            bucket["owed_to_us"] = quant_money(bucket["owed_to_us"] + stat.expected_remaining)
        else:
            bucket["we_owe"] = quant_money(bucket["we_owe"] + stat.expected_remaining)
    for bucket in saldo_by_cp.values():
        bucket["net"] = quant_money(bucket["owed_to_us"] - bucket["we_owe"])
    counterparty_saldo = sorted(
        saldo_by_cp.values(),
        key=lambda b: abs(b["net"]),
        reverse=True,
    )

    # Подсказка по статусу: что разумно сделать со статусом кейса сейчас?
    suggested_status = None
    suggested_status_reason = ""
    if case.status in ("active", "waiting_for_inflow"):
        all_settled = (
            available_now >= MONEY_ZERO
            and expected_inflow_remaining <= MONEY_ZERO
            and expected_outflow_remaining <= MONEY_ZERO
        )
        if all_settled:
            suggested_status = "closed"
            suggested_status_reason = "все обязательства закрыты, деньги в плюсе — кейс можно завершить"
        elif case.status == "active" and expected_inflow_remaining > MONEY_ZERO and available_now < MONEY_ZERO:
            suggested_status = "waiting_for_inflow"
            suggested_status_reason = "деньги ушли, но ждём поступление по доходным договорам — перевести в ожидание возврата"
        elif case.status == "waiting_for_inflow" and available_now >= MONEY_ZERO:
            suggested_status = "active"
            suggested_status_reason = "поступление пришло, кассовый разрыв закрыт — вернуть в активную работу"

    # Хронология событий кейса — единая визуальная лента «что произошло когда»:
    # привязка договоров, факты прихода/расхода, погашения займов, статусы.
    timeline_events: list[dict] = []
    for c in contracts:
        timeline_events.append({
            "date": c.date,
            "kind": "contract",
            "icon": _contract_icon(c.kind),
            "title": f"{_contract_kind_label(c.kind)} {c.number}",
            "subtitle": f"{c.counterparty.name} · {quant_money(c.amount)} {c.currency.code}",
            "tone": _contract_tone(c.kind),
            "object_id": c.id,
        })
    for fact in facts:
        is_loan_repayment = fact.loan_repayment_contract_id in contract_stats
        if fact.direction == PaymentDirection.INFLOW:
            icon, tone, title = "↓", "success", "Поступление"
        elif is_loan_repayment:
            icon, tone, title = "↩", "warning", "Погашение займа"
        else:
            icon, tone, title = "↑", "danger", "Платёж"
        contract_label = fact.contract.number if fact.contract else "—"
        timeline_events.append({
            "date": fact.date,
            "kind": "fact",
            "icon": icon,
            "title": f"{title} · {fact.amount} {fact.currency.code}",
            "subtitle": f"{contract_label} · {fact.counterparty.name} · {fact.article.code} {fact.article.name}",
            "tone": tone,
            "object_id": fact.id,
            "interest_portion": fact.loan_interest_portion if is_loan_repayment else None,
        })
    # Подтянуть переходы статусов кейса из AuditLog (видим всю канву проекта)
    from core.models import AuditLog
    status_logs = (
        AuditLog.objects.filter(
            object_type="FundingCase",
            object_id=str(case.pk),
        )
        .filter(message__icontains="статус кейса")
        .order_by("created_at")
    )
    for log in status_logs:
        timeline_events.append({
            "date": log.created_at.date(),
            "kind": "status",
            "icon": "🚦",
            "title": log.message,
            "subtitle": f"автор: {log.user.username if log.user else 'система'}",
            "tone": "info",
            "object_id": log.id,
        })

    # Сортируем по дате (от ранних к поздним — естественный порядок чтения timeline)
    timeline_events.sort(key=lambda e: (e["date"], 0 if e["kind"] == "contract" else (2 if e["kind"] == "status" else 1)))

    # Sankey: расходимся в три колонки — источники → кейс → получатели
    sankey = _build_sankey(contracts, facts, contract_stats, case)

    return {
        "case": case,
        "contracts_by_kind": dict(by_kind),
        "counterparty_saldo": counterparty_saldo,
        "suggested_status": suggested_status,
        "suggested_status_reason": suggested_status_reason,
        "timeline_events": timeline_events,
        "sankey": sankey,
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
