"""Domain permission helpers — thin wrappers over Django model permissions.

Keep this module free of view imports so it can be safely imported from any
view module without creating cycles.
"""

from ..models import (
    BudgetLimitAdjustment,
    BudgetLimitPlan,
    BudgetPlan,
    PaymentFact,
    PaymentRequest,
    UserRole,
)
from ._shared import has_model_permission


def can_edit_plans(user) -> bool:
    return has_model_permission(user, BudgetLimitPlan, "change")


def can_create_budgets(user) -> bool:
    return has_model_permission(user, BudgetPlan, "add")


def can_approve_budgets(user) -> bool:
    return can_approve_plans(user) and has_model_permission(user, BudgetPlan, "view")


def can_create_plans(user) -> bool:
    return has_model_permission(user, BudgetLimitPlan, "add")


def can_create_limit_adjustments(user) -> bool:
    return has_model_permission(user, BudgetLimitAdjustment, "add")


def can_edit_limit_adjustments(user) -> bool:
    return has_model_permission(user, BudgetLimitAdjustment, "change")


def can_delete_plans(user) -> bool:
    return has_model_permission(user, BudgetLimitPlan, "delete")


def can_approve_plans(user) -> bool:
    if user.is_superuser:
        return True
    profile = getattr(user, "profile", None)
    return bool(profile and profile.role in [UserRole.ADMINISTRATOR, UserRole.MANAGER])


def can_approve_payment_requests(user) -> bool:
    return can_approve_plans(user) and has_model_permission(user, PaymentRequest, "view")


def can_manage_payment_requests(user) -> bool:
    return has_model_permission(user, PaymentRequest, "change")


def can_create_payment_requests(user) -> bool:
    return has_model_permission(user, PaymentRequest, "add")


def can_delete_payment_requests(user) -> bool:
    return has_model_permission(user, PaymentRequest, "delete")


def can_edit_payment_facts(user) -> bool:
    return has_model_permission(user, PaymentFact, "change")


def can_view_payment_facts(user) -> bool:
    return has_model_permission(user, PaymentFact, "view")


def can_create_payment_facts(user) -> bool:
    return has_model_permission(user, PaymentFact, "add")


def can_delete_payment_facts(user) -> bool:
    return has_model_permission(user, PaymentFact, "delete")
