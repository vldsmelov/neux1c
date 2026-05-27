
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from core.context_processors import ui_theme
from core.models import (
    UiThemeMode,
    UiThemeSettings,
    UserProfile,
    UserRole,
)



class UiThemeSettingsTests(TestCase):
    def test_active_theme_exposes_css_variables(self):
        theme = UiThemeSettings.objects.create(
            name="Custom",
            is_active=True,
            primary_color="#006ecb",
        )

        variables = theme.as_css_variables()

        self.assertEqual(variables["--app-primary"], "#006ecb")
        self.assertIn("--app-bg", variables)

    def test_only_one_theme_stays_active(self):
        first = UiThemeSettings.objects.create(name="First", is_active=True)
        second = UiThemeSettings.objects.create(name="Second", is_active=True)

        first.refresh_from_db()
        second.refresh_from_db()

        self.assertFalse(first.is_active)
        self.assertTrue(second.is_active)

    def test_dark_mode_changes_css_variables(self):
        User = get_user_model()
        user = User.objects.create_user(username="theme_user", password="demo12345")
        UserProfile.objects.create(user=user, role=UserRole.ECONOMIST, theme_mode=UiThemeMode.DARK)
        request = RequestFactory().get("/")
        request.user = user
        request.session = {}

        payload = ui_theme(request)

        self.assertEqual(payload["ui_theme_mode"], UiThemeMode.DARK)
        self.assertEqual(payload["ui_theme"]["--app-bg"], "#111a26")
        self.assertEqual(payload["ui_theme"]["--app-surface"], "#182433")


