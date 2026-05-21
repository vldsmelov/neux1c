"""Authentication & per-session preference views.

Кратко: вход (с role-aware-редиректом), переключение темы интерфейса и выбор
рабочей компании. Все три не привязаны к доменным сервисам и могут жить
изолированно.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from ..models import Organization, UiThemeMode, UserRole


class RoleAwareLoginView(LoginView):
    template_name = "registration/login.html"

    def get_success_url(self):
        profile = getattr(self.request.user, "profile", None)
        if profile and profile.role == UserRole.ACCOUNTANT:
            return reverse("external_accounting")
        return reverse("workspace")


@login_required
def set_theme_mode(request):
    mode = request.POST.get("theme_mode")
    if mode not in UiThemeMode.values:
        mode = UiThemeMode.LIGHT

    profile = getattr(request.user, "profile", None)
    if profile:
        profile.theme_mode = mode
        profile.save(update_fields=["theme_mode", "updated_at"])
    request.session["ui_theme_mode"] = mode

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or reverse("workspace")
    if not url_has_allowed_host_and_scheme(next_url, {request.get_host()}):
        next_url = reverse("workspace")
    return redirect(next_url)


@login_required
def set_working_organization(request):
    organization = get_object_or_404(Organization, pk=request.POST.get("organization_id"))
    request.session["working_organization_id"] = organization.id
    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or reverse("workspace")
    if not url_has_allowed_host_and_scheme(next_url, {request.get_host()}):
        next_url = reverse("workspace")
    messages.success(request, f"Рабочая компания: {organization.name}")
    return redirect(next_url)
