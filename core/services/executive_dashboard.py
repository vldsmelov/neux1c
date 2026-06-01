"""Executive Dashboard — CFO/CEO view верхнего уровня.

Один экран — всё состояние холдинга:
* Cash position (приход − расход YTD)
* Прибыль по начислению YTD
* Активные кейсы + кейсы в дефиците
* Сумма к возврату по займам
* Просроченные заявки
* Тренд cash position по месяцам года
"""

from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Sum

from core.models import (
    Contract,
    ContractKind,
    FundingCase,
    FundingCaseStatus,
    PaymentDirection,
    PaymentFact,
    PaymentRequest,
    PaymentRequestStatus,
)
from core.services.funding_cases import build_case_overview


MONEY_ZERO = Decimal("0.00")


def build_executive_dashboard(year: int, organization_id: int | None = None) -> dict:
    """Полная сводка для дашборда CFO."""
    fact_q = PaymentFact.objects.filter(date__year=year)
    if organization_id:
        fact_q = fact_q.filter(organization_id=organization_id)

    cash_in = fact_q.filter(direction=PaymentDirection.INFLOW).aggregate(t=Sum("amount"))["t"] or MONEY_ZERO
    cash_out = fact_q.filter(direction=PaymentDirection.OUTFLOW).aggregate(t=Sum("amount"))["t"] or MONEY_ZERO
    cash_position = cash_in - cash_out

    # Accrued P&L
    contracts_q = Contract.objects.filter(date__year=year)
    income = contracts_q.filter(kind=ContractKind.CUSTOMER).aggregate(t=Sum("amount"))["t"] or MONEY_ZERO
    expense = contracts_q.filter(kind=ContractKind.SOLE_SUPPLIER).aggregate(t=Sum("amount"))["t"] or MONEY_ZERO
    accrued_profit = income - expense

    # Funding cases
    cases_q = FundingCase.objects.filter(status=FundingCaseStatus.ACTIVE)
    if organization_id:
        cases_q = cases_q.filter(organization_id=organization_id)
    active_cases = cases_q.count()
    deficit_cases = 0
    deficit_total = MONEY_ZERO
    top_risks: list[dict] = []
    for c in cases_q.select_related("organization"):
        ov = build_case_overview(c)
        if ov["projected_balance"] < 0:
            deficit_cases += 1
            deficit_total += ov["projected_balance"]
            top_risks.append({
                "kind": "case_deficit",
                "label": f"Кейс {c.code} в дефиците",
                "amount": ov["projected_balance"],
                "link": f"/funding/cases/{c.id}/",
            })

    # Loans outstanding (сумма всех expected_remaining по LOAN_RECEIVED активных кейсов)
    loans_total_remaining = MONEY_ZERO
    loans_q = Contract.objects.filter(
        kind=ContractKind.LOAN_RECEIVED,
        funding_case__isnull=False,
        funding_case__status=FundingCaseStatus.ACTIVE,
    )
    if organization_id:
        loans_q = loans_q.filter(funding_case__organization_id=organization_id)
    today = date.today()
    horizon_30 = today + timedelta(days=30)
    loans_due_soon = []
    for loan in loans_q.select_related("counterparty", "funding_case"):
        principal_left = loan.amount
        # Берём грубую оценку: amount - paid_outflow по этому контракту
        paid = (
            PaymentFact.objects.filter(loan_repayment_contract=loan)
            .aggregate(t=Sum("amount"))["t"] or MONEY_ZERO
        )
        principal_left = max(MONEY_ZERO, loan.amount - paid)
        # Простая аппроксимация — только тело без процентов
        loans_total_remaining += principal_left
        if loan.maturity_date and today <= loan.maturity_date <= horizon_30:
            loans_due_soon.append({
                "loan": loan,
                "remaining": principal_left,
                "days_left": (loan.maturity_date - today).days,
            })
            top_risks.append({
                "kind": "loan_due",
                "label": f"Возврат займа {loan.number} через {(loan.maturity_date - today).days} дн.",
                "amount": principal_left,
                "link": f"/funding/cases/{loan.funding_case_id}/",
            })

    # Overdue payment requests
    overdue_q = PaymentRequest.objects.filter(
        status__in=[PaymentRequestStatus.PENDING_APPROVAL, PaymentRequestStatus.PENDING_FINAL_APPROVAL],
        submitted_at__lt=today - timedelta(days=3),
    )
    if organization_id:
        overdue_q = overdue_q.filter(organization_id=organization_id)
    overdue_count = overdue_q.count()
    overdue_amount = overdue_q.aggregate(t=Sum("amount_rub"))["t"] or MONEY_ZERO
    if overdue_count:
        top_risks.append({
            "kind": "request_overdue",
            "label": f"{overdue_count} заявок просрочено (>3 дней на согласовании)",
            "amount": overdue_amount,
            "link": "/payments/requests/",
        })

    # Cash position trend by month
    monthly_trend = _build_monthly_trend(fact_q, year)

    # Сортировка рисков по сумме (от худшего)
    top_risks.sort(key=lambda r: r["amount"])

    return {
        "year": year,
        "organization_id": organization_id,
        "cash_in": cash_in,
        "cash_out": cash_out,
        "cash_position": cash_position,
        "income": income,
        "expense": expense,
        "accrued_profit": accrued_profit,
        "active_cases": active_cases,
        "deficit_cases": deficit_cases,
        "deficit_total": deficit_total,
        "loans_total_remaining": loans_total_remaining,
        "loans_due_soon": loans_due_soon,
        "overdue_count": overdue_count,
        "overdue_amount": overdue_amount,
        "top_risks": top_risks[:8],
        "monthly_trend": monthly_trend,
    }


def _build_monthly_trend(fact_q, year: int) -> dict:
    """Cash position по месяцам: cumulative приход − расход.
    Возвращает данные для SVG-line chart: точки (x, y) + bounds."""
    monthly_in = {m: MONEY_ZERO for m in range(1, 13)}
    monthly_out = {m: MONEY_ZERO for m in range(1, 13)}
    for fact in fact_q.values("date__month", "direction").annotate(t=Sum("amount")):
        m = fact["date__month"]
        if fact["direction"] == PaymentDirection.INFLOW:
            monthly_in[m] = fact["t"]
        else:
            monthly_out[m] = fact["t"]

    # Cumulative position
    points = []
    cum = MONEY_ZERO
    for m in range(1, 13):
        cum += monthly_in[m] - monthly_out[m]
        points.append({"month": m, "cash_in": monthly_in[m], "cash_out": monthly_out[m], "position": cum})

    if not any(p["position"] != 0 or p["cash_in"] != 0 or p["cash_out"] != 0 for p in points):
        return {"empty": True}

    all_positions = [float(p["position"]) for p in points]
    min_pos = min(all_positions + [0])  # включаем 0 чтобы график не «съезжал»
    max_pos = max(all_positions + [0])
    span = max_pos - min_pos or 1

    # SVG canvas 700×220
    canvas_w = 700
    canvas_h = 220
    pad_left = 60
    pad_right = 20
    pad_top = 20
    pad_bottom = 30
    plot_w = canvas_w - pad_left - pad_right
    plot_h = canvas_h - pad_top - pad_bottom

    def x_at(month):
        return pad_left + (month - 1) * plot_w / 11

    def y_at(value):
        return pad_top + plot_h - (float(value) - min_pos) / span * plot_h

    # Zero-line
    zero_y = y_at(0) if min_pos < 0 < max_pos else None

    return {
        "empty": False,
        "canvas_w": canvas_w,
        "canvas_h": canvas_h,
        "points": [{
            "x": round(x_at(p["month"]), 1),
            "y": round(y_at(p["position"]), 1),
            "month": p["month"],
            "label": _month_label(p["month"]),
            "position": p["position"],
            "cash_in": p["cash_in"],
            "cash_out": p["cash_out"],
        } for p in points],
        "zero_y": round(zero_y, 1) if zero_y is not None else None,
        "min_pos": min_pos,
        "max_pos": max_pos,
        "pad_left": pad_left,
        "pad_bottom": pad_bottom,
        "plot_w": plot_w,
        "plot_h": plot_h,
    }


def _month_label(month: int) -> str:
    return ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"][month - 1]
