from django.db import OperationalError, ProgrammingError

from .models import Organization, UiThemeMode, UiThemeSettings


DARK_OVERRIDES = {
    "--app-bg": "#111a26",
    "--app-surface": "#182433",
    "--app-sidebar": "#142030",
    "--app-border": "#2d4158",
    "--app-text": "#e5edf8",
    "--app-muted": "#9bb0c7",
    "--app-primary-soft": "#224063",
    "--app-toolbar": "#162232",
    "--app-control": "#0f1b2a",
    "--app-row-hover": "#203349",
}


def _resolve_theme_mode(request) -> str:
    session = getattr(request, "session", {})
    session_mode = session.get("ui_theme_mode")
    if session_mode in UiThemeMode.values:
        return session_mode
    if request.user.is_authenticated:
        profile = getattr(request.user, "profile", None)
        if profile and profile.theme_mode in UiThemeMode.values:
            return profile.theme_mode
    return UiThemeMode.LIGHT


def ui_theme(request):
    try:
        theme = UiThemeSettings.objects.filter(is_active=True).first()
    except (OperationalError, ProgrammingError):
        theme = None

    if theme is None:
        theme = UiThemeSettings.default()

    variables = theme.as_css_variables()
    variables.setdefault("--app-toolbar", "#f4f7fb")
    variables.setdefault("--app-control", "#ffffff")
    variables.setdefault("--app-row-hover", "#eef7ff")

    theme_mode = _resolve_theme_mode(request)
    if theme_mode == UiThemeMode.DARK:
        variables.update(DARK_OVERRIDES)

    return {
        "ui_theme": variables,
        "ui_theme_mode": theme_mode,
    }


def notifications_summary(request):
    """Expose unread count + the latest few notifications for the header dropdown."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"unread_notifications": 0, "recent_notifications": []}
    try:
        from .models import Notification
        from .services.notifications import unread_count

        recent = list(
            Notification.objects.filter(recipient=user).order_by("-created_at")[:6]
        )
        return {
            "unread_notifications": unread_count(user),
            "recent_notifications": recent,
        }
    except (OperationalError, ProgrammingError):
        return {"unread_notifications": 0, "recent_notifications": []}


def current_user_flags(request):
    """Expose role-derived booleans to every template (e.g. for sidebar gating).

    Computed once per request; values are cheap (no extra DB queries beyond
    the AuthenticationMiddleware-loaded profile).
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"is_admin_user": False}
    if user.is_superuser:
        return {"is_admin_user": True}
    profile = getattr(user, "profile", None)
    return {"is_admin_user": bool(profile and profile.role == "administrator")}


def working_organization(request):
    try:
        organizations = list(Organization.objects.order_by("name"))
    except (OperationalError, ProgrammingError):
        organizations = []

    selected = None
    selected_id = None
    session = getattr(request, "session", None)
    if session is not None:
        selected_id = session.get("working_organization_id")
    if selected_id:
        selected = next((organization for organization in organizations if organization.id == selected_id), None)
    if selected is None and organizations:
        selected = organizations[0]
        if session is not None:
            session["working_organization_id"] = selected.id

    return {
        "organizations_for_switch": organizations,
        "working_organization": selected,
    }
