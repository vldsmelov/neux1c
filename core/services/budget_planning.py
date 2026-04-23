from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core.models import AuditAction, AuditLog, BudgetLimitMonth, BudgetLimitPlan, BudgetPlanStatus


MONTH_NUMBERS = list(range(1, 13))


@transaction.atomic
def create_budget_plan(
    *,
    author,
    department,
    article,
    currency,
    planning_year: int,
    planning_horizon: int,
    annual_amount: Decimal,
    approver,
    monthly_amounts: dict[int, Decimal] | None = None,
    comment: str = "",
) -> BudgetLimitPlan:
    if planning_horizon not in (1, 2, 3):
        raise ValueError("Горизонт планирования должен быть 1, 2 или 3 года")

    monthly_amounts = monthly_amounts or split_amount_by_months(annual_amount)
    validate_monthly_amounts(annual_amount, monthly_amounts)

    plan = BudgetLimitPlan.objects.create(
        number=next_budget_plan_number(),
        document_date=timezone.localdate(),
        planning_year=planning_year,
        planning_horizon=planning_horizon,
        department=department,
        article=article,
        currency=currency,
        annual_amount=annual_amount,
        approver=approver,
        author=author,
        comment=comment,
    )
    BudgetLimitMonth.objects.bulk_create(
        [BudgetLimitMonth(plan=plan, month=month, amount=monthly_amounts[month]) for month in MONTH_NUMBERS]
    )

    AuditLog.objects.create(
        user=author,
        action=AuditAction.CREATE,
        path="/planning/limits/",
        object_type="BudgetLimitPlan",
        object_id=str(plan.pk),
        message=f"Создан план {plan.number}",
    )
    return plan


def split_amount_by_months(annual_amount: Decimal) -> dict[int, Decimal]:
    base = (annual_amount / Decimal("12")).quantize(Decimal("0.01"))
    amounts = {month: base for month in MONTH_NUMBERS}
    amounts[12] = annual_amount - sum(amounts[month] for month in MONTH_NUMBERS[:-1])
    return amounts


def validate_monthly_amounts(annual_amount: Decimal, monthly_amounts: dict[int, Decimal]) -> None:
    if set(monthly_amounts.keys()) != set(MONTH_NUMBERS):
        raise ValueError("План должен содержать 12 месяцев")
    if sum(monthly_amounts.values()) != annual_amount:
        raise ValueError("Сумма месяцев должна быть равна годовой сумме")


@transaction.atomic
def submit_budget_plan(plan: BudgetLimitPlan, user) -> BudgetLimitPlan:
    if plan.status != BudgetPlanStatus.DRAFT:
        raise ValueError("На утверждение можно отправить только черновик")
    if not plan.is_monthly_total_valid:
        raise ValueError("Сумма месяцев должна быть равна годовой сумме")

    plan.status = BudgetPlanStatus.PENDING_APPROVAL
    plan.save(update_fields=["status", "updated_at"])
    AuditLog.objects.create(
        user=user,
        action=AuditAction.UPDATE,
        path="/planning/limits/",
        object_type="BudgetLimitPlan",
        object_id=str(plan.pk),
        message=f"План {plan.number} отправлен на утверждение",
    )
    return plan


@transaction.atomic
def approve_budget_plan(plan: BudgetLimitPlan, user) -> BudgetLimitPlan:
    if plan.status != BudgetPlanStatus.PENDING_APPROVAL:
        raise ValueError("Утвердить можно только план на утверждении")
    if plan.approver_id != user.id and not user.is_superuser:
        raise ValueError("План может утвердить только назначенный руководитель")

    plan.status = BudgetPlanStatus.APPROVED
    plan.approved_at = timezone.now()
    plan.save(update_fields=["status", "approved_at", "updated_at"])
    AuditLog.objects.create(
        user=user,
        action=AuditAction.APPROVE,
        path="/planning/limits/",
        object_type="BudgetLimitPlan",
        object_id=str(plan.pk),
        message=f"План {plan.number} утвержден",
    )
    return plan


def next_budget_plan_number() -> str:
    return f"PL-{timezone.localdate():%Y}-{BudgetLimitPlan.objects.count() + 1:06d}"
