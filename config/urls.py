from django.contrib import admin
from django.contrib.auth.views import LogoutView
from django.urls import path

from core.views import RoleAwareLoginView, external_accounting, healthz, nsi_dashboard, workspace


urlpatterns = [
    path("", workspace, name="workspace"),
    path("nsi/", nsi_dashboard, name="nsi_dashboard"),
    path("external/accounting/", external_accounting, name="external_accounting"),
    path("login/", RoleAwareLoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("healthz/", healthz, name="healthz"),
    path("admin/", admin.site.urls),
]
