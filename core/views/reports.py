"""Reports: plan-fact (БДДС) с шаблонами и dashboard руководителя."""

from urllib.parse import urlencode

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from ..access import role_required
from ..models import (
    CashFlowArticle,
    Contract,
    ContractKind,
    Counterparty,
    Organization,
    PaymentRequestStatus,
    ReportTemplate,
    ReportTemplateType,
    UserRole,
)
from ..services.manager_dashboard import build_manager_dashboard
from ..services.plan_fact_report import PlanFactFilters, build_plan_fact_report, to_csv as plan_fact_to_csv
from ._shared import has_model_permission, parse_optional_int, working_organization


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def plan_fact_report(request):
    org = working_organization(request)
    current_year = timezone.localdate().year
    templates = list(
        ReportTemplate.objects.filter(
            owner=request.user, report_type=ReportTemplateType.PLAN_FACT
        ).order_by("name")
    )
    templates_by_id = {template.id: template for template in templates}
    default_template = next((template for template in templates if template.is_default), None)

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "apply_template":
                template = _resolve_report_template(templates_by_id, request.POST.get("template_id"))
                query = _plan_fact_payload_to_query(template.filters, template_id=template.id)
                return redirect(f"{reverse('plan_fact_report')}?{query}")

            if action in {"save_template", "delete_template"} and not _can_manage_report_templates(request.user):
                raise ValueError("Сохранять и удалять шаблоны может только пользователь с правом создания шаблонов")

            if action == "save_template":
                template_name = (request.POST.get("template_name") or "").strip()
                if not template_name:
                    raise ValueError("Укажите название шаблона")
                payload = _collect_plan_fact_filter_payload(request.POST, default_year=current_year)
                template, created = ReportTemplate.objects.update_or_create(
                    owner=request.user,
                    report_type=ReportTemplateType.PLAN_FACT,
                    name=template_name,
                    defaults={
                        "filters": payload,
                        "is_default": request.POST.get("is_default") == "1",
                    },
                )
                messages.success(request, "Шаблон отчета создан" if created else "Шаблон отчета обновлен")
                query = _plan_fact_payload_to_query(payload, template_id=template.id)
                return redirect(f"{reverse('plan_fact_report')}?{query}")
            if action == "delete_template":
                template = _resolve_report_template(templates_by_id, request.POST.get("template_id"))
                template.delete()
                messages.success(request, "Шаблон отчета удален")
                return redirect("plan_fact_report")
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("plan_fact_report")

    selected_template_id = parse_optional_int(request.GET.get("template_id"))
    selected_template = templates_by_id.get(selected_template_id) if selected_template_id else None

    base_filters = {}
    if selected_template:
        base_filters = selected_template.filters or {}
    elif not request.GET and default_template:
        base_filters = default_template.filters or {}
        selected_template = default_template
        selected_template_id = default_template.id

    selected_year = _read_filter_int(request.GET, base_filters, "year") or current_year
    selected_status = _read_filter_text(
        request.GET, base_filters, "request_status", allowed_values=PaymentRequestStatus.values,
    )
    selected_article_id = _read_filter_int(request.GET, base_filters, "article_id")
    selected_counterparty_id = _read_filter_int(request.GET, base_filters, "counterparty_id")
    selected_organization_id = _read_filter_int(request.GET, base_filters, "organization_id") or org.id
    selected_customer_contract_id = _read_filter_int(request.GET, base_filters, "customer_contract_id")
    selected_supplier_contract_id = _read_filter_int(request.GET, base_filters, "supplier_contract_id")

    filters = PlanFactFilters(
        year=selected_year,
        article_id=selected_article_id,
        counterparty_id=selected_counterparty_id,
        organization_id=selected_organization_id,
        customer_contract_id=selected_customer_contract_id,
        supplier_contract_id=selected_supplier_contract_id,
        request_status=selected_status,
    )
    rows, summary = build_plan_fact_report(filters)

    if request.GET.get("export") == "excel":
        response = HttpResponse(plan_fact_to_csv(rows), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="plan_fact_{selected_year}.csv"'
        return response

    return render(
        request,
        "core/plan_fact_report.html",
        {
            "active_section": "reports",
            "rows": rows,
            "summary": summary,
            "years": list(range(current_year - 1, current_year + 3)),
            "articles": CashFlowArticle.objects.order_by("code"),
            "organizations": Organization.objects.order_by("name"),
            "counterparties": Counterparty.objects.order_by("name"),
            "customer_contracts": Contract.objects.filter(kind=ContractKind.CUSTOMER).order_by("number"),
            "supplier_contracts": Contract.objects.filter(kind=ContractKind.SOLE_SUPPLIER).order_by("number"),
            "request_status_choices": [("", "Все статусы")] + list(PaymentRequestStatus.choices),
            "selected_year": selected_year,
            "selected_article_id": selected_article_id,
            "selected_counterparty_id": selected_counterparty_id,
            "selected_organization_id": selected_organization_id,
            "selected_customer_contract_id": selected_customer_contract_id,
            "selected_supplier_contract_id": selected_supplier_contract_id,
            "selected_request_status": selected_status,
            "report_templates": templates,
            "selected_template_id": selected_template_id,
            "working_organization": org,
            "can_manage_report_templates": _can_manage_report_templates(request.user),
            "active_filters_payload": {
                "year": selected_year,
                "article_id": selected_article_id,
                "organization_id": selected_organization_id,
                "counterparty_id": selected_counterparty_id,
                "customer_contract_id": selected_customer_contract_id,
                "supplier_contract_id": selected_supplier_contract_id,
                "request_status": selected_status,
            },
        },
    )


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def manager_dashboard(request):
    org = working_organization(request)
    current_year = timezone.localdate().year
    selected_year = parse_optional_int(request.GET.get("year")) or current_year
    payload = build_manager_dashboard(year=selected_year, organization_id=org.id)
    return render(
        request,
        "core/manager_dashboard.html",
        {
            "active_section": "reports",
            "years": list(range(current_year - 1, current_year + 3)),
            "selected_year": selected_year,
            "working_organization": org,
            **payload,
        },
    )


# --- Helpers (module-private) --------------------------------------------

def _resolve_report_template(templates_by_id: dict[int, ReportTemplate], raw_template_id) -> ReportTemplate:
    template_id = parse_optional_int(raw_template_id)
    if not template_id:
        raise ValueError("Выберите шаблон отчета")
    template = templates_by_id.get(template_id)
    if template is None:
        raise ValueError("Шаблон отчета не найден")
    return template


def _read_filter_int(query_params, base_filters: dict, key: str) -> int | None:
    if key in query_params:
        return parse_optional_int(query_params.get(key))
    return parse_optional_int(base_filters.get(key))


def _read_filter_text(query_params, base_filters: dict, key: str, allowed_values) -> str:
    if key in query_params:
        raw_value = (query_params.get(key) or "").strip()
    else:
        fallback = base_filters.get(key)
        raw_value = str(fallback).strip() if fallback is not None else ""
    if raw_value not in allowed_values:
        return ""
    return raw_value


def _collect_plan_fact_filter_payload(data, *, default_year: int) -> dict:
    year = parse_optional_int(data.get("year")) or default_year
    request_status = (data.get("request_status") or "").strip()
    if request_status not in PaymentRequestStatus.values:
        request_status = ""
    return {
        "year": year,
        "article_id": parse_optional_int(data.get("article_id")),
        "counterparty_id": parse_optional_int(data.get("counterparty_id")),
        "organization_id": parse_optional_int(data.get("organization_id")),
        "customer_contract_id": parse_optional_int(data.get("customer_contract_id")),
        "supplier_contract_id": parse_optional_int(data.get("supplier_contract_id")),
        "request_status": request_status,
    }


def _plan_fact_payload_to_query(payload: dict, template_id: int | None = None) -> str:
    query = {}
    for key, value in payload.items():
        if value in (None, ""):
            continue
        query[key] = str(value)
    if template_id:
        query["template_id"] = str(template_id)
    return urlencode(query)


def _can_manage_report_templates(user) -> bool:
    return has_model_permission(user, ReportTemplate, "add")
