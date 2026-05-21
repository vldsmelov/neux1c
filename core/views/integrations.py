"""Integration request views: journal + creation wizard + admin status wizard."""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from ..access import role_required
from ..models import IntegrationRequest, IntegrationRequestStatus, UserRole
from ..services.integration_requests import (
    create_integration_request,
    update_integration_request_status,
)
from ._shared import is_administrator


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def integration_requests(request):
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "create":
                create_integration_request(
                    requested_by=request.user,
                    integration_name=request.POST.get("integration_name", ""),
                    target_system=request.POST.get("target_system", ""),
                    description=request.POST.get("description", ""),
                )
                messages.success(request, "Заявка на интеграцию создана")
            elif action == "set_status":
                if not is_administrator(request.user):
                    raise ValueError("Изменять статус заявки может только администратор")
                update_integration_request_status(
                    request=get_object_or_404(IntegrationRequest, pk=request.POST.get("request_id")),
                    admin_user=request.user,
                    status=request.POST.get("status", ""),
                    admin_comment=request.POST.get("admin_comment", ""),
                )
                messages.success(request, "Статус заявки обновлен")
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect("integration_requests")

    selected_status = (request.GET.get("status") or "").strip()
    if selected_status not in IntegrationRequestStatus.values:
        selected_status = ""

    requests_query = IntegrationRequest.objects.select_related("requested_by", "assigned_admin")
    if selected_status:
        requests_query = requests_query.filter(status=selected_status)
    requests_list = list(requests_query)

    return render(
        request,
        "core/integration_requests.html",
        {
            "active_section": "settings",
            "requests": requests_list,
            "status_choices": [("", "Все статусы")] + list(IntegrationRequestStatus.choices),
            "selected_status": selected_status,
            "can_manage_integration_requests": is_administrator(request.user),
        },
    )


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def integration_request_wizard(request):
    submitted: dict = {}
    if request.method == "POST":
        try:
            integration_request = create_integration_request(
                requested_by=request.user,
                integration_name=request.POST.get("integration_name", ""),
                target_system=request.POST.get("target_system", ""),
                description=request.POST.get("description", ""),
            )
            messages.success(request, f"Заявка {integration_request.number} создана")
            return redirect("integration_requests")
        except ValueError as exc:
            messages.error(request, str(exc))
            submitted = request.POST

    return render(
        request,
        "core/integration_request_wizard.html",
        {"active_section": "settings", "submitted": submitted},
    )


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def integration_request_status_wizard(request, request_id: int):
    integration_request = get_object_or_404(IntegrationRequest, pk=request_id)
    submitted: dict = {}
    if request.method == "POST":
        try:
            if not is_administrator(request.user):
                raise ValueError("Изменять статус заявки может только администратор")
            update_integration_request_status(
                request=integration_request,
                admin_user=request.user,
                status=request.POST.get("status", ""),
                admin_comment=request.POST.get("admin_comment", ""),
            )
            messages.success(request, f"Статус заявки {integration_request.number} обновлен")
            return redirect("integration_requests")
        except ValueError as exc:
            messages.error(request, str(exc))
            submitted = request.POST

    return render(
        request,
        "core/integration_request_status_wizard.html",
        {
            "active_section": "settings",
            "item": integration_request,
            "status_choices": list(IntegrationRequestStatus.choices),
            "can_manage_integration_requests": is_administrator(request.user),
            "submitted": submitted,
        },
    )
