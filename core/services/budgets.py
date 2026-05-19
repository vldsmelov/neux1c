from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core.models import (
    AuditAction,
    AuditLog,
    BudgetDepartmentAllocation,
    BudgetLimitPlan,
    BudgetPlan,
    BudgetPlanStatus,
    BudgetScope,
    Organization,
)
from core.services.document_numbers import next_document_number
from core.services.payment_requests import quant_money


WORKFLOW_PATH = "/planning/budgets/"
WIZARD_WORKFLOW_PATH = "/planning/wizard/"


@transaction.atomic
def create_budget(
    *,
    author,
    organization=None,
    budget_year: int,
    scope: str,
    currency,
    total_amount: Decimal | None = None,
    department_amounts: dict[int, Decimal] | None = None,
    comment: str = "",
) -> BudgetPlan:
    organization = organization or _default_organization()
    if scope not in BudgetScope.values:
        raise ValueError("Выберите тип бюджета")

    department_amounts = {
        department_id: amount
        for department_id, amount in (department_amounts or {}).items()
        if amount is not None and amount != Decimal("0")
    }

    if scope == BudgetScope.OVERALL:
        if total_amount is None:
            raise ValueError("Укажите сумму общего бюджета")
        if total_amount <= 0:
            raise ValueError("Сумма бюджета должна быть больше нуля")
        normalized_total = quant_money(total_amount)
        department_amounts = {}
    else:
        if not department_amounts:
            raise ValueError("Для бюджета по ЦФО заполните хотя бы одну строку")
        if any(amount <= 0 for amount in department_amounts.values()):
            raise ValueError("Сумма бюджета ЦФО должна быть больше нуля")
        normalized_total = quant_money(sum(department_amounts.values(), Decimal("0")))

    budget = BudgetPlan.objects.create(
        number=next_document_number("BUD"),
        document_date=timezone.localdate(),
        organization=organization,
        budget_year=budget_year,
        scope=scope,
        currency=currency,
        total_amount=normalized_total,
        author=author,
        comment=comment,
    )
    BudgetDepartmentAllocation.objects.bulk_create(
        [
            BudgetDepartmentAllocation(
                budget=budget,
                department_id=department_id,
                amount=quant_money(amount),
            )
            for department_id, amount in department_amounts.items()
        ]
    )

    AuditLog.objects.create(
        user=author,
        action=AuditAction.CREATE,
        path=WORKFLOW_PATH,
        object_type="BudgetPlan",
        object_id=str(budget.pk),
        message=f"Создан бюджет {budget.number}",
    )
    return budget


@transaction.atomic
def approve_budget(budget: BudgetPlan, user) -> BudgetPlan:
    if budget.status == BudgetPlanStatus.CLOSED:
        raise ValueError("Закрытый бюджет нельзя утверждать повторно")

    budget.status = BudgetPlanStatus.APPROVED
    budget.save(update_fields=["status", "updated_at"])
    AuditLog.objects.create(
        user=user,
        action=AuditAction.APPROVE,
        path=WORKFLOW_PATH,
        object_type="BudgetPlan",
        object_id=str(budget.pk),
        message=f"Бюджет {budget.number} утвержден",
    )
    return budget


@transaction.atomic
def submit_budget(budget: BudgetPlan, user) -> BudgetPlan:
    if budget.status != BudgetPlanStatus.DRAFT:
        raise ValueError("На утверждение можно отправить только черновик бюджета")

    budget.status = BudgetPlanStatus.PENDING_APPROVAL
    budget.save(update_fields=["status", "updated_at"])
    AuditLog.objects.create(
        user=user,
        action=AuditAction.UPDATE,
        path=WORKFLOW_PATH,
        object_type="BudgetPlan",
        object_id=str(budget.pk),
        message=f"Бюджет {budget.number} отправлен на утверждение",
    )
    return budget


@transaction.atomic
def create_primary_budget_package(
    *,
    author,
    organization=None,
    budget_year: int,
    scope: str,
    currency,
    approver,
    total_amount: Decimal | None = None,
    department_amounts: dict[int, Decimal] | None = None,
    limit_rows: list[dict] | None = None,
    comment: str = "",
) -> tuple[BudgetPlan, list[BudgetLimitPlan]]:
    if not limit_rows:
        raise ValueError("Добавьте минимум один лимит")

    from core.services.budget_planning import create_budget_plan, submit_budget_plan

    budget = create_budget(
        author=author,
        organization=organization,
        budget_year=budget_year,
        scope=scope,
        currency=currency,
        total_amount=total_amount,
        department_amounts=department_amounts,
        comment=comment,
    )
    submit_budget(budget, author)

    limits = []
    for row in limit_rows:
        plan = create_budget_plan(
            author=author,
            budget=budget,
            department=row["department"],
            article=row["article"],
            currency=currency,
            planning_year=budget_year,
            planning_horizon=1,
            annual_amount=row["annual_amount"],
            approver=approver,
            monthly_amounts=row.get("monthly_amounts"),
            comment=row.get("comment") or comment,
        )
        limits.append(submit_budget_plan(plan, author))

    AuditLog.objects.create(
        user=author,
        action=AuditAction.UPDATE,
        path=WIZARD_WORKFLOW_PATH,
        object_type="BudgetPlan",
        object_id=str(budget.pk),
        message=f"Первичный пакет {budget.number} отправлен на утверждение, лимитов: {len(limits)}",
    )
    return budget, limits


def validate_limit_within_budget(
    *,
    budget: BudgetPlan | None,
    department,
    annual_amount: Decimal,
    exclude_plan_id: int | None = None,
) -> None:
    if budget is None:
        return
    if annual_amount <= 0:
        raise ValueError("Сумма лимита должна быть больше нуля")

    remaining = budget_remaining_for_limit(
        budget=budget,
        department=department,
        exclude_plan_id=exclude_plan_id,
    )
    if quant_money(annual_amount) > remaining:
        raise ValueError(
            f"Лимит превышает доступный бюджет. Доступно {remaining}, запрошено {quant_money(annual_amount)}"
        )


def budget_remaining_for_limit(
    *,
    budget: BudgetPlan,
    department,
    exclude_plan_id: int | None = None,
) -> Decimal:
    used = _used_limit_amount(budget=budget, department=department, exclude_plan_id=exclude_plan_id)

    if budget.scope == BudgetScope.OVERALL:
        available = budget.total_amount
    else:
        allocation = budget.department_allocations.filter(department=department).first()
        if allocation is None:
            raise ValueError("Для выбранного ЦФО нет строки бюджета")
        available = allocation.amount

    return quant_money(available - used)


def build_budget_rows(*, organization=None) -> list[dict]:
    budgets = BudgetPlan.objects.select_related("organization", "currency", "author").prefetch_related(
        "department_allocations__department",
        "limits",
    )
    if organization is not None:
        budgets = budgets.filter(organization=organization)
    rows = []
    for budget in budgets:
        used_total = _used_limit_amount(budget=budget, department=None)
        reserve_total = quant_money(budget.total_amount - used_total)
        department_rows = _department_budget_rows(budget)
        rows.append(
            {
                "budget": budget,
                "used_total": used_total,
                "reserve_total": reserve_total,
                "department_rows": department_rows,
            }
        )
    return rows


def build_budget_summary(rows: list[dict]) -> dict:
    return {
        "budgets": len(rows),
        "total_budget": quant_money(sum((row["budget"].total_amount for row in rows), Decimal("0"))),
        "total_limits": quant_money(sum((row["used_total"] for row in rows), Decimal("0"))),
        "total_reserve": quant_money(sum((row["reserve_total"] for row in rows), Decimal("0"))),
    }


def _department_budget_rows(budget: BudgetPlan) -> list[dict]:
    if budget.scope != BudgetScope.BY_DEPARTMENT:
        return []

    used_by_department = _used_limit_amounts_by_department(budget)
    rows = []
    for allocation in budget.department_allocations.all():
        used = used_by_department.get(allocation.department_id, Decimal("0"))
        rows.append(
            {
                "allocation": allocation,
                "used": quant_money(used),
                "reserve": quant_money(allocation.amount - used),
            }
        )
    return rows


def _used_limit_amount(*, budget: BudgetPlan, department, exclude_plan_id: int | None = None) -> Decimal:
    query = BudgetLimitPlan.objects.filter(budget=budget).exclude(status=BudgetPlanStatus.CLOSED)
    if department is not None:
        query = query.filter(department=department)
    if exclude_plan_id:
        query = query.exclude(pk=exclude_plan_id)
    return query.aggregate(total=Sum("annual_amount")).get("total") or Decimal("0")


def _used_limit_amounts_by_department(budget: BudgetPlan) -> dict[int, Decimal]:
    rows = (
        BudgetLimitPlan.objects.filter(budget=budget)
        .exclude(status=BudgetPlanStatus.CLOSED)
        .values("department_id")
        .annotate(total=Sum("annual_amount"))
    )
    result = defaultdict(lambda: Decimal("0"))
    for row in rows:
        result[row["department_id"]] = row["total"] or Decimal("0")
    return dict(result)


def _default_organization():
    organization = Organization.objects.order_by("id").first()
    if organization is None:
        raise ValueError("Создайте минимум одну компанию")
    return organization
