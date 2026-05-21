"""Shared view helpers reused across the split view modules.

Keep this module small and free of domain logic — it must not import from any
other view sub-module to avoid circular imports.
"""

from datetime import date, datetime
from decimal import Decimal

from ..access import user_has_role
from ..models import Organization, UserRole


# --- Role / permission checks ---------------------------------------------

def is_administrator(user) -> bool:
    if user.is_superuser:
        return True
    profile = getattr(user, "profile", None)
    return bool(profile and profile.role == UserRole.ADMINISTRATOR)


def can_sync_mock_1c(user) -> bool:
    return user_has_role(user, {UserRole.ADMINISTRATOR, UserRole.ECONOMIST})


def has_model_permission(user, model, action: str) -> bool:
    if not user.is_authenticated or not user.is_active:
        return False
    if user.is_superuser:
        return True
    return user.has_perm(f"{model._meta.app_label}.{action}_{model._meta.model_name}")


# --- Session / working context --------------------------------------------

def working_organization(request):
    """Resolve and persist the currently selected working organization."""
    organization_id = request.session.get("working_organization_id")
    organization = None
    if organization_id:
        organization = Organization.objects.filter(pk=organization_id).first()
    if organization is None:
        organization = Organization.objects.order_by("name").first()
    if organization is None:
        return None
    request.session["working_organization_id"] = organization.id
    return organization


# --- Lightweight POST parsers ---------------------------------------------

def parse_decimal(raw_value):
    if not raw_value:
        return None
    return Decimal(raw_value.replace(" ", "").replace(",", "."))


def parse_optional_int(raw_value):
    if not raw_value:
        return None
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


def parse_optional_date(raw_value: str | None) -> date | None:
    if not raw_value:
        return None
    try:
        return date.fromisoformat(raw_value)
    except ValueError:
        pass
    try:
        return datetime.strptime(raw_value, "%d.%m.%Y").date()
    except ValueError:
        return None
