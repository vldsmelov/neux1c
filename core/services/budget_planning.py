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
    NotificationKind,
    Organization,
)
from core.services.budgets import validate_limit_within_budget
from core.services.document_numbers import next_document_number
from core.services.notifications import notify
from core.services.payment_requests import recalculate_pending_requests_for_limit


MONTH_NUMBERS = list(range(1, 13))


@transaction.atomic
def create_budget_plan(
    *,
    author,
    budget=None,
    organization=None,
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
    organization = budget.organization if budget is not None else (organization or _default_organization())
    if planning_horizon not in (1, 2, 3):
        raise ValueError("Горизонт планирования должен быть 1, 2 или 3 года")

    monthly_amounts = monthly_amounts or split_amount_by_months(annual_amount)
    validate_monthly_amounts(annual_amount, monthly_amounts)
    validate_limit_within_budget(budget=budget, department=department, annual_amount=annual_amount)

    plan = BudgetLimitPlan.objects.create(
        number=next_budget_plan_number(),
        document_date=timezone.localdate(),
        organization=organization,
        budget=budget,
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
    if plan.approver_id:
        notify(
            recipient=plan.approver,
            kind=NotificationKind.LIMIT_SUBMITTED,
            title=f"Лимит {plan.number} на утверждении",
            text=f"{plan.department.name} · {plan.article.name} · {plan.annual_amount} {plan.currency.code}",
            link="/planning/limits/",
            related=plan,
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
    notify(
        recipient=plan.author,
        kind=NotificationKind.LIMIT_APPROVED,
        title=f"Лимит {plan.number} утверждён",
        text=f"{plan.department.name} · {plan.article.name} · {plan.annual_amount} {plan.currency.code}",
        link="/planning/limits/",
        related=plan,
    )
    # Каскад: новый approved-лимит может изменить статус превышения у
    # уже отправленных pending-заявок по этой же статье + компании.
    recalculate_pending_requests_for_limit(
        article=plan.article,
        organization=plan.organization,
    )
    return plan


def next_budget_plan_number() -> str:
    return next_document_number("PL")


@transaction.atomic
def create_limit_adjustment(
    *,
    author,
    base_plan: BudgetLimitPlan,
    new_annual_amount: Decimal,
    reason: str,
    monthly_amounts: dict[int, Decimal] | None = None,
    target_plan: BudgetLimitPlan | None = None,
) -> BudgetLimitAdjustment:
    if base_plan.status != BudgetPlanStatus.APPROVED:
        raise ValueError("Корректировать можно только утвержденный план")
    if not reason.strip():
        raise ValueError("Причина корректировки обязательна")

    monthly_amounts = monthly_amounts or split_amount_by_months(new_annual_amount)
    validate_monthly_amounts(new_annual_amount, monthly_amounts)
    transfer_amount = Decimal("0")
    if target_plan is not None:
        if target_plan.status != BudgetPlanStatus.APPROVED:
            raise ValueError("Целевой лимит для переноса должен быть утвержден")
        if target_plan.id == base_plan.id:
            raise ValueError("Для межкомпанийного переноса выберите другой целевой лимит")
        if target_plan.organization_id == base_plan.organization_id:
            raise ValueError("Межкомпанийный перенос требует лимит другой компании")
        if target_plan.article_id != base_plan.article_id:
            raise ValueError("Перенос между компаниями возможен только по той же статье ДДС")
        transfer_amount = base_plan.annual_amount - new_annual_amount
        if transfer_amount <= 0:
            raise ValueError("Для переноса в другую компанию новая сумма базового лимита должна быть меньше текущей")
        validate_limit_within_budget(
            budget=target_plan.budget,
            department=target_plan.department,
            annual_amount=target_plan.annual_amount + transfer_amount,
            exclude_plan_id=target_plan.id,
        )
    validate_limit_within_budget(
        budget=base_plan.budget,
        department=base_plan.department,
        annual_amount=new_annual_amount,
        exclude_plan_id=base_plan.id,
    )

    adjustment = BudgetLimitAdjustment.objects.create(
        number=next_adjustment_number(),
        document_date=timezone.localdate(),
        base_plan=base_plan,
        article=base_plan.article,
        target_plan=target_plan,
        target_organization=target_plan.organization if target_plan else None,
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
def create_limit_adjustment_request(
    *,
    author,
    base_plan: BudgetLimitPlan,
    new_annual_amount: Decimal,
    reason: str,
    monthly_amounts: dict[int, Decimal] | None = None,
    target_plan: BudgetLimitPlan | None = None,
) -> BudgetLimitAdjustment:
    adjustment = create_limit_adjustment(
        author=author,
        base_plan=base_plan,
        new_annual_amount=new_annual_amount,
        reason=reason,
        monthly_amounts=monthly_amounts,
        target_plan=target_plan,
    )
    return submit_limit_adjustment(adjustment, author)


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
    if adjustment.approver_id:
        notify(
            recipient=adjustment.approver,
            kind=NotificationKind.LIMIT_ADJUSTMENT_SUBMITTED,
            title=f"Корректировка {adjustment.number} на утверждении",
            text=(
                f"Базовый лимит: {adjustment.base_plan.number} · "
                f"новая сумма: {adjustment.new_annual_amount} {adjustment.base_plan.currency.code}"
            ),
            link="/planning/limits/",
            related=adjustment,
        )
    return adjustment


@transaction.atomic
def approve_limit_adjustment(adjustment: BudgetLimitAdjustment, user) -> BudgetLimitAdjustment:
    if adjustment.status != BudgetPlanStatus.PENDING_APPROVAL:
        raise ValueError("Утвердить можно только корректировку на утверждении")
    if adjustment.approver_id != user.id and not user.is_superuser:
        raise ValueError("Корректировку может утвердить только назначенный руководитель")

    validate_limit_within_budget(
        budget=adjustment.base_plan.budget,
        department=adjustment.base_plan.department,
        annual_amount=adjustment.new_annual_amount,
        exclude_plan_id=adjustment.base_plan_id,
    )

    adjustment.status = BudgetPlanStatus.APPROVED
    adjustment.approved_at = timezone.now()
    adjustment.save(update_fields=["status", "approved_at", "updated_at"])

    base_plan = adjustment.base_plan
    previous_base_amount = base_plan.annual_amount
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

    if adjustment.target_plan_id:
        target_plan = adjustment.target_plan
        transfer_amount = previous_base_amount - adjustment.new_annual_amount
        if transfer_amount <= 0:
            raise ValueError("Сумма межкомпанийного переноса должна быть больше нуля")
        validate_limit_within_budget(
            budget=target_plan.budget,
            department=target_plan.department,
            annual_amount=target_plan.annual_amount + transfer_amount,
            exclude_plan_id=target_plan.id,
        )
        target_plan.annual_amount += transfer_amount
        target_plan.version += 1
        target_plan.correction_reason = f"Межкомпанийный перенос из {base_plan.number}"[:255]
        additions = split_amount_by_months(transfer_amount)
        existing_months = {row.month: row for row in target_plan.months.all()}
        for month, amount in additions.items():
            if month in existing_months:
                row = existing_months[month]
                row.amount += amount
                row.save(update_fields=["amount"])
            else:
                BudgetLimitMonth.objects.create(plan=target_plan, month=month, amount=amount)
        target_plan.save(update_fields=["annual_amount", "version", "correction_reason", "updated_at"])

    AuditLog.objects.create(
        user=user,
        action=AuditAction.APPROVE,
        path="/planning/limits/",
        object_type="BudgetLimitAdjustment",
        object_id=str(adjustment.pk),
        message=f"Корректировка {adjustment.number} утверждена",
    )
    # Каскад на pending-заявки: меняется approved-сумма базового лимита и
    # (опционально) целевого лимита межкомпанийного переноса.
    recalculate_pending_requests_for_limit(
        article=base_plan.article,
        organization=base_plan.organization,
    )
    if adjustment.target_plan_id:
        recalculate_pending_requests_for_limit(
            article=adjustment.target_plan.article,
            organization=adjustment.target_plan.organization,
        )
    return adjustment


def next_adjustment_number() -> str:
    return next_document_number("ADJ")


def _default_organization():
    organization = Organization.objects.order_by("id").first()
    if organization is None:
        raise ValueError("Создайте минимум одну компанию")
    return organization
