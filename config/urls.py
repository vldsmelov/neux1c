from django.contrib import admin
from django.urls import path

from core.views import healthz, workspace


urlpatterns = [
    path("", workspace, name="workspace"),
    path("healthz/", healthz, name="healthz"),
    path("admin/", admin.site.urls),
]
