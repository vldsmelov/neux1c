"""Аннуитетный график платежей по займу.

PMT = P × r × (1+r)^n / ((1+r)^n − 1)
где P — тело, r — месячная ставка (annual/12/100), n — число месяцев.

Каждый период:
* interest_due = balance × r
* principal_due = PMT − interest_due
* balance_after = balance − principal_due
"""

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction

from core.models import Contract, ContractKind, LoanScheduleLine, PaymentFact


MONEY_QUANT = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _add_months(date_value, months: int):
    """Прибавляет N месяцев к дате (грубо — через timedelta 30 дней).

    Для финансовых графиков можно усложнить до календарных месяцев,
    но 30-дневный шаг — достаточно близко и предсказуемо."""
    return date_value + timedelta(days=30 * months)


@transaction.atomic
def generate_annuity_schedule(loan: Contract, periods: int) -> list[LoanScheduleLine]:
    """Генерирует строки графика. Удаляет существующие и создаёт заново.

    periods — количество месяцев (равных периодов).
    """
    if loan.kind not in (ContractKind.LOAN_RECEIVED, ContractKind.LOAN_GIVEN):
        raise ValueError("График можно сгенерировать только для договора займа")
    if periods <= 0:
        raise ValueError("Количество периодов должно быть больше нуля")
    if not loan.maturity_date:
        raise ValueError("На договоре займа должна быть дата возврата (maturity_date)")

    principal = Decimal(loan.amount)
    annual_rate = Decimal(loan.interest_rate or 0)
    monthly_rate = annual_rate / Decimal("12") / Decimal("100")

    if monthly_rate == 0:
        # Беспроцентный заём: одинаковые платежи только по телу
        pmt = principal / Decimal(periods)
    else:
        # Аннуитет: PMT = P × r × (1+r)^n / ((1+r)^n − 1)
        one_plus_r_n = (Decimal("1") + monthly_rate) ** periods
        pmt = principal * monthly_rate * one_plus_r_n / (one_plus_r_n - Decimal("1"))

    # Стартовая дата для шага: следующий месяц после подписания
    base_date = loan.date

    loan.schedule_lines.all().delete()

    balance = principal
    rows: list[LoanScheduleLine] = []
    for i in range(1, periods + 1):
        interest = _money(balance * monthly_rate)
        principal_part = _money(pmt - interest) if monthly_rate else _money(pmt)
        # На последнем периоде «дочистить» округление до 0
        if i == periods:
            principal_part = balance
            interest = _money(balance * monthly_rate) if monthly_rate else Decimal("0.00")
        balance_after = _money(balance - principal_part)
        if balance_after < 0:
            balance_after = Decimal("0.00")

        row = LoanScheduleLine.objects.create(
            loan_contract=loan,
            period=i,
            due_date=_add_months(base_date, i),
            principal_due=principal_part,
            interest_due=interest,
            balance_after=balance_after,
        )
        rows.append(row)
        balance = balance_after

    return rows


@transaction.atomic
def apply_repayment_to_schedule(fact: PaymentFact) -> int:
    """Распределяет факт возврата по строкам графика, отмечая их paid.

    Идём по периодам с ранней даты, для каждой не оплаченной строки
    закрываем сначала проценты, потом тело, пока не исчерпаем сумму факта.
    Возвращает число затронутых строк.
    """
    if not fact.loan_repayment_contract_id:
        return 0
    loan = fact.loan_repayment_contract
    lines = list(
        loan.schedule_lines
        .filter(paid_at__isnull=True)
        .order_by("period")
    )
    if not lines:
        return 0

    remaining_total = Decimal(fact.amount)
    touched = 0
    for line in lines:
        line_due = line.total_due - line.paid_amount
        if remaining_total <= 0:
            break
        applied = min(remaining_total, line_due)
        line.paid_amount = _money(line.paid_amount + applied)
        remaining_total -= applied
        if line.paid_amount >= line.total_due:
            line.paid_at = fact.date
        line.save(update_fields=["paid_amount", "paid_at"])
        touched += 1
    return touched
