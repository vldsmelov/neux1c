from django.contrib import admin
from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from core.views import healthz, nsi_dashboard, workspace


urlpatterns = [
    path("", workspace, name="workspace"),
    path("nsi/", nsi_dashboard, name="nsi_dashboard"),
    path("login/", LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("healthz/", healthz, name="healthz"),
    path("admin/", admin.site.urls),
]
