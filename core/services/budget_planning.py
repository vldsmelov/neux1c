from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core.models import (
    AuditAction,
    AuditLog,
    BudgetLimitAdjustment,
    BudgetLimitAdjustmentMonth,
    BudgetLimitMonth,
    BudgetLimitPlan,
    BudgetPlanStatus,
)


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


@transaction.atomic
def create_limit_adjustment(
    *,
    author,
    base_plan: BudgetLimitPlan,
    new_annual_amount: Decimal,
    reason: str,
    monthly_amounts: dict[int, Decimal] | None = None,
) -> BudgetLimitAdjustment:
    if base_plan.status != BudgetPlanStatus.APPROVED:
        raise ValueError("Корректировать можно только утвержденный план")
    if not reason.strip():
        raise ValueError("Причина корректировки обязательна")

    monthly_amounts = monthly_amounts or {month.month: month.amount for month in base_plan.months.all()}
    validate_monthly_amounts(new_annual_amount, monthly_amounts)

    adjustment = BudgetLimitAdjustment.objects.create(
        number=next_adjustment_number(),
        document_date=timezone.localdate(),
        base_plan=base_plan,
        article=base_plan.article,
        new_annual_amount=new_annual_amount,
        reason=reason,
        version=base_plan.version + 1,
        approver=base_plan.approver,
        author=author,
    )
    BudgetLimitAdjustmentMonth.objects.bulk_create(
        [
            BudgetLimitAdjustmentMonth(adjustment=adjustment, month=month, amount=monthly_amounts[month])
            for month in MONTH_NUMBERS
        ]
    )
    AuditLog.objects.create(
        user=author,
        action=AuditAction.CREATE,
        path="/planning/limits/",
        object_type="BudgetLimitAdjustment",
        object_id=str(adjustment.pk),
        message=f"Создана корректировка {adjustment.number}",
    )
    return adjustment


@transaction.atomic
def submit_limit_adjustment(adjustment: BudgetLimitAdjustment, user) -> BudgetLimitAdjustment:
    if adjustment.status != BudgetPlanStatus.DRAFT:
        raise ValueError("На утверждение можно отправить только черновик")
    if not adjustment.is_monthly_total_valid:
        raise ValueError("Сумма месяцев должна быть равна новой годовой сумме")

    adjustment.status = BudgetPlanStatus.PENDING_APPROVAL
    adjustment.save(update_fields=["status", "updated_at"])
    AuditLog.objects.create(
        user=user,
        action=AuditAction.UPDATE,
        path="/planning/limits/",
        object_type="BudgetLimitAdjustment",
        object_id=str(adjustment.pk),
        message=f"Корректировка {adjustment.number} отправлена на утверждение",
    )
    return adjustment


@transaction.atomic
def approve_limit_adjustment(adjustment: BudgetLimitAdjustment, user) -> BudgetLimitAdjustment:
    if adjustment.status != BudgetPlanStatus.PENDING_APPROVAL:
        raise ValueError("Утвердить можно только корректировку на утверждении")
    if adjustment.approver_id != user.id and not user.is_superuser:
        raise ValueError("Корректировку может утвердить только назначенный руководитель")

    adjustment.status = BudgetPlanStatus.APPROVED
    adjustment.approved_at = timezone.now()
    adjustment.save(update_fields=["status", "approved_at", "updated_at"])

    base_plan = adjustment.base_plan
    base_plan.annual_amount = adjustment.new_annual_amount
    base_plan.version = adjustment.version
    base_plan.correction_reason = adjustment.reason[:255]
    base_plan.months.all().delete()
    BudgetLimitMonth.objects.bulk_create(
        [
            BudgetLimitMonth(plan=base_plan, month=row.month, amount=row.amount)
            for row in adjustment.months.all()
        ]
    )
    base_plan.save(update_fields=["annual_amount", "version", "correction_reason", "updated_at"])

    AuditLog.objects.create(
        user=user,
        action=AuditAction.APPROVE,
        path="/planning/limits/",
        object_type="BudgetLimitAdjustment",
        object_id=str(adjustment.pk),
        message=f"Корректировка {adjustment.number} утверждена",
    )
    return adjustment


def next_adjustment_number() -> str:
    return f"ADJ-{timezone.localdate():%Y}-{BudgetLimitAdjustment.objects.count() + 1:06d}"
