"""Сценарии планирования: CRUD-страница в разделе Планирование."""

from django.contrib import messages
from django.db import IntegrityError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from ..access import role_required
from ..models import (
    AuditAction,
    AuditLog,
    Organization,
    PlanningScenario,
    PlanningScenarioKind,
    UserRole,
)
from ._permissions import can_create_plans
from ._shared import parse_optional_int, working_organization


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def planning_scenarios(request):
    org = working_organization(request)
    current_year = timezone.localdate().year
    selected_year = parse_optional_int(request.GET.get("year")) or current_year
    selected_org_id = parse_optional_int(request.GET.get("organization_id")) or (org.id if org else None)

    can_edit = can_create_plans(request.user)

    if request.method == "POST":
        if not can_edit:
            messages.error(request, "Недостаточно прав для управления сценариями планирования")
            return redirect("planning_scenarios")

        action = request.POST.get("action")
        try:
            if action == "create":
                _create_scenario(request)
                messages.success(request, "Сценарий создан")
            elif action == "update":
                _update_scenario(request)
                messages.success(request, "Сценарий обновлён")
            elif action == "set_baseline":
                scenario = get_object_or_404(PlanningScenario, pk=request.POST.get("scenario_id"))
                scenario.is_baseline = True
                scenario.save(update_fields=["is_baseline"])
                AuditLog.objects.create(
                    user=request.user,
                    action=AuditAction.UPDATE,
                    path="/planning/scenarios/",
                    object_type="PlanningScenario",
                    object_id=str(scenario.pk),
                    message=f"Сценарий {scenario.name} ({scenario.year}, {scenario.organization.name}) сделан базовым",
                )
                messages.success(request, f"«{scenario.name}» теперь базовый сценарий")
            elif action == "delete":
                _delete_scenario(request)
                messages.success(request, "Сценарий удалён")
            else:
                raise ValueError("Неизвестное действие")
        except (IntegrityError, ValueError) as exc:
            messages.error(request, _format_error(exc))
        return redirect(
            f"/planning/scenarios/?year={selected_year}"
            + (f"&organization_id={selected_org_id}" if selected_org_id else "")
        )

    scenarios_qs = PlanningScenario.objects.select_related("organization", "author").order_by(
        "-year", "organization__name", "-is_baseline", "name"
    )
    if selected_org_id:
        scenarios_qs = scenarios_qs.filter(organization_id=selected_org_id)
    if selected_year:
        scenarios_qs = scenarios_qs.filter(year=selected_year)

    return render(
        request,
        "core/planning_scenarios.html",
        {
            "active_section": "planning",
            "scenarios": list(scenarios_qs),
            "organizations": Organization.objects.order_by("name"),
            "kinds": PlanningScenarioKind.choices,
            "years": list(range(current_year - 1, current_year + 3)),
            "selected_year": selected_year,
            "selected_organization_id": selected_org_id,
            "working_organization": org,
            "can_edit": can_edit,
        },
    )


def _create_scenario(request) -> PlanningScenario:
    name = (request.POST.get("name") or "").strip()
    if not name:
        raise ValueError("Укажите название сценария")
    year = parse_optional_int(request.POST.get("year"))
    if not year:
        raise ValueError("Укажите год")
    org_id = parse_optional_int(request.POST.get("organization_id"))
    organization = get_object_or_404(Organization, pk=org_id)
    kind = request.POST.get("kind") or PlanningScenarioKind.CUSTOM
    if kind not in PlanningScenarioKind.values:
        kind = PlanningScenarioKind.CUSTOM
    is_baseline = request.POST.get("is_baseline") == "1"

    scenario = PlanningScenario.objects.create(
        name=name,
        year=year,
        organization=organization,
        kind=kind,
        is_baseline=is_baseline,
        comment=(request.POST.get("comment") or "").strip(),
        author=request.user,
    )
    AuditLog.objects.create(
        user=request.user,
        action=AuditAction.CREATE,
        path="/planning/scenarios/",
        object_type="PlanningScenario",
        object_id=str(scenario.pk),
        message=f"Создан сценарий {scenario}",
    )
    return scenario


def _update_scenario(request) -> PlanningScenario:
    scenario = get_object_or_404(PlanningScenario, pk=request.POST.get("scenario_id"))
    name = (request.POST.get("name") or "").strip()
    if not name:
        raise ValueError("Укажите название сценария")
    scenario.name = name
    scenario.comment = (request.POST.get("comment") or "").strip()
    kind = request.POST.get("kind")
    if kind in PlanningScenarioKind.values:
        scenario.kind = kind
    scenario.is_baseline = request.POST.get("is_baseline") == "1"
    scenario.save(update_fields=["name", "comment", "kind", "is_baseline", "updated_at"])
    AuditLog.objects.create(
        user=request.user,
        action=AuditAction.UPDATE,
        path="/planning/scenarios/",
        object_type="PlanningScenario",
        object_id=str(scenario.pk),
        message=f"Обновлён сценарий {scenario}",
    )
    return scenario


def _delete_scenario(request) -> None:
    scenario = get_object_or_404(PlanningScenario, pk=request.POST.get("scenario_id"))
    if scenario.is_baseline:
        raise ValueError("Базовый сценарий удалить нельзя — сначала назначьте базовым другой")
    if scenario.budgets.exists() or scenario.limits.exists():
        raise ValueError("Сценарий используется в бюджетах или лимитах и не может быть удалён")
    name = str(scenario)
    pk = str(scenario.pk)
    scenario.delete()
    AuditLog.objects.create(
        user=request.user,
        action=AuditAction.DELETE,
        path="/planning/scenarios/",
        object_type="PlanningScenario",
        object_id=pk,
        message=f"Удалён сценарий {name}",
    )


def _format_error(exc) -> str:
    if isinstance(exc, IntegrityError):
        return "Сценарий с таким названием в этой компании на этот год уже существует"
    return str(exc)
