from django.contrib import admin
from django.contrib.auth.views import LogoutView
from django.urls import path

from core.views import (
    RoleAwareLoginView,
    contracts_reservations,
    contracts_tree,
    external_accounting,
    healthz,
    integration_requests,
    manager_dashboard,
    nsi_dashboard,
    payment_facts,
    plan_fact_report,
    set_theme_mode,
    payment_requests,
    planning_limits,
    workspace,
)


urlpatterns = [
    path("", workspace, name="workspace"),
    path("planning/limits/", planning_limits, name="planning_limits"),
    path("payments/requests/", payment_requests, name="payment_requests"),
    path("payments/facts/", payment_facts, name="payment_facts"),
    path("reports/manager/", manager_dashboard, name="manager_dashboard"),
    path("reports/plan-fact/", plan_fact_report, name="plan_fact_report"),
    path("contracts/reservations/", contracts_reservations, name="contracts_reservations"),
    path("contracts/tree/", contracts_tree, name="contracts_tree"),
    path("nsi/", nsi_dashboard, name="nsi_dashboard"),
    path("external/accounting/", external_accounting, name="external_accounting"),
    path("login/", RoleAwareLoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("settings/theme/", set_theme_mode, name="set_theme_mode"),
    path("settings/integration-requests/", integration_requests, name="integration_requests"),
    path("healthz/", healthz, name="healthz"),
    path("admin/", admin.site.urls),
]
