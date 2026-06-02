"""UI Driver-based Planning /planning/drivers/."""

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from ..access import role_required
from ..models import Organization, PlanDriver, UserRole
from ..services.driver_planning import compute_formula
from ._shared import parse_optional_int


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def drivers_index(request):
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "create":
                _create_driver(request)
                messages.success(request, "Драйвер создан")
            elif action == "delete":
                driver = get_object_or_404(
                    PlanDriver, pk=parse_optional_int(request.POST.get("driver_id"))
                )
                name = driver.name
                driver.delete()
                messages.success(request, f"Драйвер «{name}» удалён")
        except (ValueError, InvalidOperation) as exc:
            messages.error(request, str(exc))
        return redirect("drivers_index")

    current_year = timezone.localdate().year
    selected_year = parse_optional_int(request.GET.get("year")) or current_year

    drivers_qs = PlanDriver.objects.select_related("organization", "created_by").filter(
        year=selected_year,
    ).order_by("organization__name", "kind", "name")

    # Preview формулы: ?formula=1:multiply,2:multiply,3:multiply
    formula_raw = (request.GET.get("formula") or "").strip()
    formula_result = None
    if formula_raw:
        steps = []
        for chunk in formula_raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if ":" in chunk:
                driver_id_str, op = chunk.split(":", 1)
            else:
                driver_id_str, op = chunk, "multiply"
            try:
                steps.append({"driver_id": int(driver_id_str), "op": op})
            except ValueError:
                continue
        if steps:
            formula_result = compute_formula(steps)

    months = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]
    drivers_with_values = []
    for d in drivers_qs:
        vals = d.monthly_values if isinstance(d.monthly_values, list) else []
        # Дополняем до 12
        normalized = []
        for i in range(12):
            try:
                normalized.append(Decimal(str(vals[i])))
            except (IndexError, InvalidOperation):
                normalized.append(Decimal("0"))
        drivers_with_values.append({
            "driver": d,
            "monthly": normalized,
            "total": d.annual_total(),
            "avg": d.annual_average(),
        })

    return render(
        request,
        "core/drivers_index.html",
        {
            "active_section": "planning",
            "organizations": Organization.objects.order_by("name"),
            "drivers_with_values": drivers_with_values,
            "selected_year": selected_year,
            "years": list(range(current_year - 1, current_year + 3)),
            "months": months,
            "kind_choices": PlanDriver.KIND_CHOICES,
            "formula_raw": formula_raw,
            "formula_result": formula_result,
        },
    )


def _create_driver(request) -> PlanDriver:
    name = (request.POST.get("name") or "").strip()
    if not name:
        raise ValueError("Укажите название драйвера")
    organization = get_object_or_404(
        Organization, pk=parse_optional_int(request.POST.get("organization_id"))
    )
    year = parse_optional_int(request.POST.get("year")) or timezone.localdate().year
    kind = request.POST.get("kind") or PlanDriver.KIND_VOLUME
    if kind not in {k for k, _ in PlanDriver.KIND_CHOICES}:
        kind = PlanDriver.KIND_VOLUME

    # 12 помесячных значений
    monthly_values = []
    for i in range(1, 13):
        raw = (request.POST.get(f"m{i}") or "0").strip().replace(",", ".")
        if not raw:
            raw = "0"
        try:
            monthly_values.append(str(Decimal(raw)))
        except InvalidOperation as exc:
            raise ValueError(f"Некорректное значение для месяца {i}: {raw}") from exc

    return PlanDriver.objects.create(
        name=name,
        code=(request.POST.get("code") or "").strip(),
        organization=organization,
        year=year,
        kind=kind,
        unit=(request.POST.get("unit") or "").strip(),
        monthly_values=monthly_values,
        comment=(request.POST.get("comment") or "").strip(),
        created_by=request.user,
    )
