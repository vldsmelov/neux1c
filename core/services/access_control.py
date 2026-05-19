from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType

from core.models import (
    AdditionalAgreement,
    AuditLog,
    BudgetDepartmentAllocation,
    BudgetLimitAdjustment,
    BudgetLimitAdjustmentMonth,
    BudgetLimitMonth,
    BudgetLimitPlan,
    BudgetPlan,
    CashFlowArticle,
    Contract,
    Counterparty,
    Currency,
    Department,
    DocumentSequence,
    ExternalPaymentDocument,
    IntegrationRequest,
    Nomenclature,
    Organization,
    PaymentFact,
    PaymentFactAdjustment,
    PaymentFactControlSettings,
    PaymentRequest,
    PaymentRequestControlSettings,
    ReportTemplate,
    SyncRun,
    UiThemeSettings,
    UserProfile,
    UserRole,
)


ROLE_GROUPS = {
    UserRole.ADMINISTRATOR: "Администратор",
    UserRole.ECONOMIST: "Экономист",
    UserRole.MANAGER: "Руководитель",
    UserRole.ACCOUNTANT: "Бухгалтер",
}

REFERENCE_MODELS = [
    Currency,
    Organization,
    Department,
    CashFlowArticle,
    Counterparty,
    Nomenclature,
    Contract,
    AdditionalAgreement,
    PaymentFact,
    PaymentFactAdjustment,
    PaymentFactControlSettings,
    BudgetPlan,
    BudgetDepartmentAllocation,
    BudgetLimitPlan,
    BudgetLimitMonth,
    BudgetLimitAdjustment,
    BudgetLimitAdjustmentMonth,
    PaymentRequest,
    PaymentRequestControlSettings,
    ReportTemplate,
    IntegrationRequest,
    ExternalPaymentDocument,
    SyncRun,
]


@dataclass(frozen=True)
class DemoUserSpec:
    username: str
    email: str
    role: str
    is_staff: bool = False
    is_superuser: bool = False
    app_access: bool = True


DEMO_USERS = [
    DemoUserSpec("admin", "admin@example.local", UserRole.ADMINISTRATOR, is_staff=True, is_superuser=True),
    DemoUserSpec("economist", "economist@example.local", UserRole.ECONOMIST, is_staff=True),
    DemoUserSpec("manager", "manager@example.local", UserRole.MANAGER, is_staff=True),
    DemoUserSpec("accountant", "accountant@example.local", UserRole.ACCOUNTANT, is_staff=False, app_access=False),
]


def setup_role_groups() -> dict[str, int]:
    groups = {role: Group.objects.get_or_create(name=name)[0] for role, name in ROLE_GROUPS.items()}

    _set_group_permissions(groups[UserRole.ADMINISTRATOR], _permissions_for_models(_all_core_models(), "all"))
    _set_group_permissions(groups[UserRole.ECONOMIST], _permissions_for_models(REFERENCE_MODELS, "edit"))
    _set_group_permissions(groups[UserRole.MANAGER], _permissions_for_models(REFERENCE_MODELS, "view"))
    _set_group_permissions(groups[UserRole.ACCOUNTANT], [])

    return {group.name: group.permissions.count() for group in groups.values()}


def setup_demo_users(password: str) -> list[str]:
    setup_role_groups()
    User = get_user_model()
    created_or_updated = []

    for spec in DEMO_USERS:
        user, _ = User.objects.update_or_create(
            username=spec.username,
            defaults={
                "email": spec.email,
                "is_active": True,
                "is_staff": spec.is_staff,
                "is_superuser": spec.is_superuser,
            },
        )
        user.set_password(password)
        user.save(update_fields=["password", "email", "is_active", "is_staff", "is_superuser"])

        group = Group.objects.get(name=ROLE_GROUPS[spec.role])
        user.groups.set([group])
        UserProfile.objects.update_or_create(
            user=user,
            defaults={
                "role": spec.role,
                "is_app_access_enabled": spec.app_access,
            },
        )
        created_or_updated.append(spec.username)

    return created_or_updated


def _set_group_permissions(group: Group, permissions) -> None:
    group.permissions.set(permissions)


def _permissions_for_models(models, mode: str):
    permissions = []
    for model in models:
        content_type = ContentType.objects.get_for_model(model)
        codenames = _permission_codenames(model, mode)
        permissions.extend(Permission.objects.filter(content_type=content_type, codename__in=codenames))
    return permissions


def _permission_codenames(model, mode: str) -> list[str]:
    model_name = model._meta.model_name
    if mode == "all":
        actions = ["add", "change", "delete", "view"]
    elif mode == "edit":
        actions = ["add", "change", "view"]
    else:
        actions = ["view"]
    return [f"{action}_{model_name}" for action in actions]


def _all_core_models():
    return REFERENCE_MODELS + [DocumentSequence, UiThemeSettings, UserProfile, AuditLog]
