from django.db import OperationalError, ProgrammingError

from .models import UiThemeSettings


def ui_theme(request):
    try:
        theme = UiThemeSettings.objects.filter(is_active=True).first()
    except (OperationalError, ProgrammingError):
        theme = None

    if theme is None:
        theme = UiThemeSettings.default()

    return {"ui_theme": theme.as_css_variables()}
