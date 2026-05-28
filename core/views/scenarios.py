"""Сценарии планирования: CRUD-страница в разделе Планирование."""

import csv
from decimal import Decimal, InvalidOperation
from io import StringIO, TextIOWrapper

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from ..access import role_required
from ..models import (
    AuditAction,
    AuditLog,
    CashFlowArticle,
    Currency,
    Department,
    Organization,
    PlanningScenario,
    PlanningScenarioKind,
    UserRole,
)
from ..services.budget_planning import MONTH_NUMBERS, create_budget_plan
from ._permissions import can_create_plans
from ._shared import parse_optional_int, working_organization


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def planning_scenarios(request):
    org = working_organization(request)
    current_year = timezone.localdate().year
    selected_year = parse_optional_int(request.GET.get("year")) or current_year
    selected_org_id = parse_optional_int(request.GET.get("organization_id")) or (org.id if org else None)

    can_edit = can_create_plans(request.user)

    if request.method == "GET" and request.GET.get("template"):
        scenario = get_object_or_404(PlanningScenario, pk=parse_optional_int(request.GET.get("template")))
        return _scenario_template_csv(scenario)

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
            elif action == "import_limits":
                stats = _import_limits_csv(request)
                messages.success(
                    request,
                    f"Импорт лимитов: создано {stats['created']}, ошибок {stats['errors']}"
                    + (f". Ошибки: {'; '.join(stats['error_messages'][:5])}" if stats["error_messages"] else ""),
                )
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


# --- Bulk limits via CSV ----------------------------------------------------

LIMIT_TEMPLATE_HEADERS = [
    "ЦФО (код)",
    "Статья ДДС (код)",
    "Валюта (код)",
    "Годовая сумма",
    "Янв", "Фев", "Мар", "Апр", "Май", "Июн",
    "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек",
    "Согласующий (логин)",
    "Комментарий",
]


def _scenario_template_csv(scenario: PlanningScenario) -> HttpResponse:
    """Скачать CSV-шаблон лимитов под сценарий с примером первой строки.

    Пример заполнен реальными кодами из НСИ компании сценария, чтобы
    финансист видел формат. Помесячные столбцы можно оставить пустыми —
    система равномерно распределит годовую сумму.
    """
    department = Department.objects.order_by("code").first()
    article = CashFlowArticle.objects.order_by("code").first()
    currency = Currency.objects.filter(code="RUB").first() or Currency.objects.order_by("code").first()

    buffer = StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(LIMIT_TEMPLATE_HEADERS)
    writer.writerow([
        department.code if department else "CFO-001",
        article.code if article else "DDS-010",
        currency.code if currency else "RUB",
        "1200000.00",
        *(["100000.00"] * 12),
        "manager",
        f"Пример строки · сценарий «{scenario.name}» · {scenario.year} · {scenario.organization.name}",
    ])
    response = HttpResponse("﻿" + buffer.getvalue(), content_type="text/csv; charset=utf-8")
    stamp = timezone.localdate().strftime("%Y%m%d")
    response["Content-Disposition"] = (
        f'attachment; filename="limits_template_{scenario.id}_{stamp}.csv"'
    )
    return response


def _import_limits_csv(request) -> dict:
    if not can_create_plans(request.user):
        raise ValueError("Недостаточно прав для импорта лимитов")
    scenario_id = parse_optional_int(request.POST.get("scenario_id"))
    if not scenario_id:
        raise ValueError("Не указан сценарий для импорта")
    scenario = get_object_or_404(PlanningScenario, pk=scenario_id)
    upload = request.FILES.get("csv_file")
    if upload is None:
        raise ValueError("Не выбран CSV-файл")

    text_stream = TextIOWrapper(upload.file, encoding="utf-8-sig", newline="")
    sample = text_stream.read(4096)
    text_stream.seek(0)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";"
    reader = csv.DictReader(text_stream, dialect=dialect)
    if reader.fieldnames is None:
        raise ValueError("CSV-файл пустой или повреждён")

    # Нормализуем заголовки: убираем регистр/пробелы для устойчивого маппинга
    header_map = {(c or "").strip().lower(): c for c in reader.fieldnames}

    def col(*candidates):
        for cand in candidates:
            key = cand.lower()
            if key in header_map:
                return header_map[key]
        return None

    department_col = col("ЦФО (код)", "ЦФО", "department")
    article_col = col("Статья ДДС (код)", "Статья ДДС", "article")
    currency_col = col("Валюта (код)", "Валюта", "currency")
    amount_col = col("Годовая сумма", "Сумма", "annual_amount")
    approver_col = col("Согласующий (логин)", "Согласующий", "approver")
    comment_col = col("Комментарий", "comment")
    month_cols = [col(name) for name in ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]]

    missing = [name for name, value in [
        ("ЦФО (код)", department_col),
        ("Статья ДДС (код)", article_col),
        ("Валюта (код)", currency_col),
        ("Годовая сумма", amount_col),
    ] if value is None]
    if missing:
        raise ValueError(f"В CSV отсутствуют обязательные колонки: {', '.join(missing)}")

    User = get_user_model()
    departments_by_code = {d.code.upper(): d for d in Department.objects.all()}
    articles_by_code = {a.code.upper(): a for a in CashFlowArticle.objects.all()}
    currencies_by_code = {c.code.upper(): c for c in Currency.objects.all()}

    created = 0
    error_messages: list[str] = []

    rows = list(reader)
    with transaction.atomic():
        for row_index, row in enumerate(rows, start=2):
            try:
                dept_code = (row.get(department_col) or "").strip().upper()
                art_code = (row.get(article_col) or "").strip().upper()
                cur_code = (row.get(currency_col) or "").strip().upper()
                amount_raw = (row.get(amount_col) or "").strip().replace(" ", "").replace(",", ".")
                if not dept_code or not art_code or not cur_code or not amount_raw:
                    raise ValueError("обязательные колонки пусты")
                department = departments_by_code.get(dept_code)
                if department is None:
                    raise ValueError(f"ЦФО «{dept_code}» не найден")
                article = articles_by_code.get(art_code)
                if article is None:
                    raise ValueError(f"Статья «{art_code}» не найдена")
                currency = currencies_by_code.get(cur_code)
                if currency is None:
                    raise ValueError(f"Валюта «{cur_code}» не найдена")
                try:
                    annual_amount = Decimal(amount_raw)
                except InvalidOperation as exc:
                    raise ValueError(f"некорректная годовая сумма «{amount_raw}»") from exc
                if annual_amount <= 0:
                    raise ValueError("годовая сумма должна быть больше нуля")

                approver_login = (row.get(approver_col) or "").strip() if approver_col else ""
                if approver_login:
                    approver = User.objects.filter(username=approver_login).first()
                    if approver is None:
                        raise ValueError(f"согласующий «{approver_login}» не найден")
                else:
                    approver = request.user

                monthly_amounts = _parse_month_values(row, month_cols, annual_amount, row_index)

                create_budget_plan(
                    author=request.user,
                    organization=scenario.organization,
                    department=department,
                    article=article,
                    currency=currency,
                    planning_year=scenario.year,
                    planning_horizon=1,
                    annual_amount=annual_amount,
                    approver=approver,
                    monthly_amounts=monthly_amounts,
                    comment=(row.get(comment_col) or "").strip() if comment_col else "",
                    scenario=scenario,
                )
                created += 1
            except (ValueError, IntegrityError) as exc:
                error_messages.append(f"строка {row_index}: {exc}")

    AuditLog.objects.create(
        user=request.user,
        action=AuditAction.CREATE,
        path="/planning/scenarios/",
        object_type="PlanningScenario",
        object_id=str(scenario.pk),
        message=(
            f"CSV-импорт лимитов в сценарий {scenario.name} ({scenario.year}, {scenario.organization.name}): "
            f"создано {created}, ошибок {len(error_messages)}"
        ),
    )
    return {"created": created, "errors": len(error_messages), "error_messages": error_messages}


def _parse_month_values(row: dict, month_cols: list, annual_amount: Decimal, row_index: int):
    """Если все 12 месяцев указаны — валидируем сумму. Иначе — None
    (сервис равномерно распределит)."""
    if not all(month_cols):
        return None
    raw_values = []
    for col_name in month_cols:
        raw = (row.get(col_name) or "").strip().replace(" ", "").replace(",", ".")
        raw_values.append(raw)
    if not any(raw_values):
        return None
    if not all(raw_values):
        raise ValueError("заполните все 12 месяцев или оставьте все месяцы пустыми")
    try:
        amounts = {month: Decimal(raw) for month, raw in zip(MONTH_NUMBERS, raw_values)}
    except InvalidOperation as exc:
        raise ValueError(f"некорректное значение в помесячных колонках: {exc}") from exc
    total = sum(amounts.values(), Decimal("0"))
    if total != annual_amount:
        raise ValueError(
            f"сумма месяцев {total} не равна годовой {annual_amount}"
        )
    return amounts
