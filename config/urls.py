from django.contrib import admin
from django.urls import path

from core.views import healthz, nsi_dashboard, workspace


urlpatterns = [
    path("", workspace, name="workspace"),
    path("nsi/", nsi_dashboard, name="nsi_dashboard"),
    path("healthz/", healthz, name="healthz"),
    path("admin/", admin.site.urls),
]
