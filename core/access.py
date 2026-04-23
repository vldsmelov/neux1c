from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

from .models import UserRole


APP_ROLES = {
    "Администратор": UserRole.ADMINISTRATOR,
    "Экономист": UserRole.ECONOMIST,
    "Руководитель": UserRole.MANAGER,
    "Бухгалтер": UserRole.ACCOUNTANT,
}


def user_has_app_access(user) -> bool:
    if not user.is_authenticated or not user.is_active:
        return False
    if user.is_superuser:
        return True

    profile = getattr(user, "profile", None)
    return bool(profile and profile.can_access_app)


def user_has_role(user, roles: set[str]) -> bool:
    if user.is_superuser:
        return True

    profile = getattr(user, "profile", None)
    return bool(profile and profile.can_access_app and profile.role in roles)


def role_required(*roles: str):
    allowed_roles = set(roles)

    def decorator(view_func):
        @login_required
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not user_has_role(request.user, allowed_roles):
                raise PermissionDenied
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator
