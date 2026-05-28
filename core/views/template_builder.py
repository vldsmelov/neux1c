"""Конструктор шаблона плана: финансист выбирает колонки и фильтры по НСИ,
система отдаёт CSV (с заголовками и опционально с пред-сгенерированными
строками-заготовками ЦФО × Статьи) — заполняешь в Excel, заливаешь
обратно через тот же import_limits на странице сценариев.
"""

import csv
from io import StringIO

from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone

from ..access import role_required
from ..models import (
    CashFlowArticle,
    Currency,
    Department,
    PlanningScenario,
    UserRole,
)
from ._permissions import can_create_plans
from ._shared import parse_optional_int, working_organization


# Колонки шаблона: ключ → подпись для CSV. Порядок важен.
TEMPLATE_COLUMNS = [
    ("department", "ЦФО (код)"),
    ("article", "Статья ДДС (код)"),
    ("currency", "Валюта (код)"),
    ("annual_amount", "Годовая сумма"),
    ("monthly", ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]),
    ("approver", "Согласующий (логин)"),
    ("comment", "Комментарий"),
]

REQUIRED_COLUMNS = {"department", "article", "currency", "annual_amount"}


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def planning_template_builder(request):
    org = working_organization(request)
    current_year = timezone.localdate().year
    can_edit = can_create_plans(request.user)

    departments = list(Department.objects.order_by("code"))
    articles = list(CashFlowArticle.objects.order_by("code"))
    currencies = list(Currency.objects.order_by("code"))
    scenarios = list(
        PlanningScenario.objects.select_related("organization")
        .filter(organization=org)
        .order_by("-year", "-is_baseline", "name")
        if org
        else PlanningScenario.objects.select_related("organization").order_by("-year", "-is_baseline", "name")
    )

    if request.method == "POST":
        # Какие колонки включать (REQUIRED включаются всегда)
        include = {key for key, _ in TEMPLATE_COLUMNS} & set(request.POST.getlist("columns"))
        include |= REQUIRED_COLUMNS

        department_ids = [int(v) for v in request.POST.getlist("department_ids") if v.isdigit()]
        article_ids = [int(v) for v in request.POST.getlist("article_ids") if v.isdigit()]
        default_currency_id = parse_optional_int(request.POST.get("default_currency_id"))
        default_approver = (request.POST.get("default_approver") or "").strip()
        generate_rows = request.POST.get("generate_rows") == "1"
        scenario_id = parse_optional_int(request.POST.get("scenario_id"))

        selected_departments = [d for d in departments if d.id in department_ids] or departments
        selected_articles = [a for a in articles if a.id in article_ids] or articles
        default_currency = next((c for c in currencies if c.id == default_currency_id), None)
        if default_currency is None:
            default_currency = next((c for c in currencies if c.code == "RUB"), currencies[0] if currencies else None)
        scenario = next((s for s in scenarios if s.id == scenario_id), None)

        return _build_template_csv(
            include=include,
            departments=selected_departments,
            articles=selected_articles,
            default_currency=default_currency,
            default_approver=default_approver,
            generate_rows=generate_rows,
            scenario=scenario,
        )

    return render(
        request,
        "core/planning_template_builder.html",
        {
            "active_section": "planning",
            "departments": departments,
            "articles": articles,
            "currencies": currencies,
            "scenarios": scenarios,
            "selectable_columns": [
                ("department", "ЦФО (код)", True),
                ("article", "Статья ДДС (код)", True),
                ("currency", "Валюта (код)", True),
                ("annual_amount", "Годовая сумма", True),
                ("monthly", "Помесячная разбивка (12 колонок)", False),
                ("approver", "Согласующий (логин)", False),
                ("comment", "Комментарий", False),
            ],
            "working_organization": org,
            "current_year": current_year,
            "can_edit": can_edit,
        },
    )


def _build_template_csv(
    *,
    include: set,
    departments: list,
    articles: list,
    default_currency,
    default_approver: str,
    generate_rows: bool,
    scenario: PlanningScenario | None,
) -> HttpResponse:
    headers: list[str] = []
    for key, label in TEMPLATE_COLUMNS:
        if key not in include:
            continue
        if key == "monthly":
            headers.extend(label)
        else:
            headers.append(label)

    buffer = StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(headers)

    if generate_rows and departments and articles:
        # Cartesian product: заготовка строк под каждое сочетание ЦФО × Статья
        for dept in departments:
            for article in articles:
                writer.writerow(_row_for(
                    include=include,
                    dept_code=dept.code,
                    article_code=article.code,
                    currency_code=default_currency.code if default_currency else "",
                    approver_login=default_approver,
                    comment="",
                ))
    else:
        # Одна строка-пример
        sample_dept = departments[0].code if departments else "CFO-001"
        sample_article = articles[0].code if articles else "DDS-010"
        sample_currency = default_currency.code if default_currency else "RUB"
        comment_text = (
            f"Пример строки · сценарий «{scenario.name}» · {scenario.year} · {scenario.organization.name}"
            if scenario is not None
            else "Пример строки · заполните годовую сумму"
        )
        writer.writerow(_row_for(
            include=include,
            dept_code=sample_dept,
            article_code=sample_article,
            currency_code=sample_currency,
            approver_login=default_approver or "manager",
            comment=comment_text,
            annual_amount_hint="1200000.00",
            monthly_hint="100000.00",
        ))

    response = HttpResponse("﻿" + buffer.getvalue(), content_type="text/csv; charset=utf-8")
    stamp = timezone.localdate().strftime("%Y%m%d")
    basename = f"plan_template_{scenario.id}_{stamp}" if scenario else f"plan_template_{stamp}"
    response["Content-Disposition"] = f'attachment; filename="{basename}.csv"'
    return response


def _row_for(
    *,
    include: set,
    dept_code: str,
    article_code: str,
    currency_code: str,
    approver_login: str,
    comment: str,
    annual_amount_hint: str = "",
    monthly_hint: str = "",
) -> list:
    row: list = []
    for key, _ in TEMPLATE_COLUMNS:
        if key not in include:
            continue
        if key == "department":
            row.append(dept_code)
        elif key == "article":
            row.append(article_code)
        elif key == "currency":
            row.append(currency_code)
        elif key == "annual_amount":
            row.append(annual_amount_hint)
        elif key == "monthly":
            row.extend([monthly_hint] * 12)
        elif key == "approver":
            row.append(approver_login)
        elif key == "comment":
            row.append(comment)
    return row
