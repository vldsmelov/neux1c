from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from mimetypes import guess_type
import html
import re
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from django.db.models import Count, Max, Q, Sum
from django.db.models.deletion import ProtectedError
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone
from django.utils.safestring import mark_safe

from .models import (
    AdditionalAgreement,
    AuditAction,
    AuditLog,
    BudgetLimitAdjustment,
    BudgetLimitPlan,
    BudgetPlan,
    BudgetPeriodicity,
    BudgetPlanStatus,
    BudgetScope,
    CashFlowArticle,
    Contract,
    ContractKind,
    Counterparty,
    Currency,
    Department,
    IntegrationRequest,
    IntegrationRequestStatus,
    Nomenclature,
    Organization,
    PaymentFact,
    PaymentFactAdjustment,
    PaymentLimitControlMode,
    PaymentRequest,
    PaymentRequestControlSettings,
    PaymentRequestKind,
    PaymentRequestStatus,
    ReportTemplate,
    ReportTemplateType,
    SourceSystem,
    SyncRun,
    UiThemeMode,
    UserRole,
)
from .access import external_accounting_required, role_required
from .integrations.one_c.mock import MockOneCProvider
from .services.budget_planning import (
    approve_budget_plan,
    approve_limit_adjustment,
    create_budget_plan,
    create_limit_adjustment,
    create_limit_adjustment_request,
    submit_budget_plan,
    submit_limit_adjustment,
)
from .services.budgets import (
    approve_budget,
    build_budget_rows,
    build_budget_summary,
    create_budget,
    create_primary_budget_package,
    submit_budget,
)
from .services.contract_reservations import build_contract_reservation_rows, build_contract_reservation_summary
from .services.contract_register import ContractRegisterFilters, build_contract_register
from .services.contract_tree import ContractTreeFilters, build_contract_tree
from .services.external_accounting import paid_amount_for_contract, pay_contract, remaining_contract_amount
from .services.integration_requests import create_integration_request, update_integration_request_status
from .services.manager_dashboard import build_manager_dashboard
from .services.payment_requests import (
    approve_payment_request,
    create_payment_request,
    reject_payment_request,
    submit_payment_request,
    transfer_payment_request_to_do,
    update_payment_request,
)
from .services.payment_fact_adjustments import adjust_payment_fact
from .services.plan_fact_report import PlanFactFilters, build_plan_fact_report, to_csv as plan_fact_to_csv
from .services.one_c_sync import sync_one_c_dataset


User = get_user_model()
BASE_DIR = Path(__file__).resolve().parent.parent
USER_GUIDE_DIR = BASE_DIR / "docs" / "user-guide"
PRIMARY_WIZARD_LIMIT_ROWS = 12
WIZARD_MONTHS = [
    (1, "Янв"),
    (2, "Фев"),
    (3, "Мар"),
    (4, "Апр"),
    (5, "Май"),
    (6, "Июн"),
    (7, "Июл"),
    (8, "Авг"),
    (9, "Сен"),
    (10, "Окт"),
    (11, "Ноя"),
    (12, "Дек"),
]


class RoleAwareLoginView(LoginView):
    template_name = "registration/login.html"

    def get_success_url(self):
        profile = getattr(self.request.user, "profile", None)
        if profile and profile.role == UserRole.ACCOUNTANT:
            return reverse("external_accounting")
        return reverse("workspace")


@login_required
def set_theme_mode(request):
    mode = request.POST.get("theme_mode")
    if mode not in UiThemeMode.values:
        mode = UiThemeMode.LIGHT

    profile = getattr(request.user, "profile", None)
    if profile:
        profile.theme_mode = mode
        profile.save(update_fields=["theme_mode", "updated_at"])
    request.session["ui_theme_mode"] = mode

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or reverse("workspace")
    if not url_has_allowed_host_and_scheme(next_url, {request.get_host()}):
        next_url = reverse("workspace")
    return redirect(next_url)


@login_required
def set_working_organization(request):
    organization = get_object_or_404(Organization, pk=request.POST.get("organization_id"))
    request.session["working_organization_id"] = organization.id
    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or reverse("workspace")
    if not url_has_allowed_host_and_scheme(next_url, {request.get_host()}):
        next_url = reverse("workspace")
    messages.success(request, f"Рабочая компания: {organization.name}")
    return redirect(next_url)


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
                if not _is_administrator(request.user):
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

    context = {
        "active_section": "settings",
        "requests": requests_list,
        "status_choices": [("", "Все статусы")] + list(IntegrationRequestStatus.choices),
        "selected_status": selected_status,
        "can_manage_integration_requests": _is_administrator(request.user),
    }
    return render(request, "core/integration_requests.html", context)


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def integration_request_wizard(request):
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
            return redirect("integration_request_wizard")

    return render(
        request,
        "core/integration_request_wizard.html",
        {"active_section": "settings"},
    )


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def integration_request_status_wizard(request, request_id: int):
    integration_request = get_object_or_404(IntegrationRequest, pk=request_id)
    if request.method == "POST":
        try:
            if not _is_administrator(request.user):
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
            return redirect("integration_request_status_wizard", request_id=integration_request.id)

    return render(
        request,
        "core/integration_request_status_wizard.html",
        {
            "active_section": "settings",
            "item": integration_request,
            "status_choices": list(IntegrationRequestStatus.choices),
            "can_manage_integration_requests": _is_administrator(request.user),
        },
    )


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def document_action_wizard(request, document_type: str, document_id: int, action: str):
    config = _document_action_config(document_type, action, document_id)
    if config is None:
        raise Http404("Document action not found")

    document = config["document"]
    can_execute = config["can_execute"](request.user, document)
    if request.method == "POST":
        try:
            if not can_execute:
                raise ValueError(config["permission_error"])
            comment = request.POST.get("comment", "")
            config["execute"](document, request.user, comment)
            messages.success(request, config["success_message"].format(number=document.number))
            return redirect(config["return_route"])
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect(
                "document_action_wizard",
                document_type=document_type,
                document_id=document_id,
                action=action,
            )

    context = {
        "active_section": config["active_section"],
        "document": document,
        "document_kind": config["document_kind"],
        "details": config["details"](document),
        "action_label": config["action_label"],
        "action_text": config["action_text"],
        "submit_label": config["submit_label"],
        "comment_label": config.get("comment_label", "Комментарий"),
        "comment_placeholder": config.get("comment_placeholder", ""),
        "comment_required": config.get("comment_required", False),
        "can_execute": can_execute,
        "return_url": reverse(config["return_route"]),
    }
    return render(request, "core/document_action_wizard.html", context)


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def workspace(request):
    modules = [
        {
            "name": "Планирование",
            "document": "Бюджет и лимит",
            "state": "В работе",
            "check": "Бюджет, резерв и контроль лимитов",
        },
        {
            "name": "НСИ",
            "document": "Синхронизация Mock-1С",
            "state": "Готово",
            "check": "Данные загружены",
        },
        {
            "name": "Договоры",
            "document": "Дерево договоров",
            "state": "НСИ готова",
            "check": "Резерв считается",
        },
        {
            "name": "Заявки",
            "document": "Заявка на оплату",
            "state": "В работе",
            "check": "Лимит + согласование + передача в 1С:ДО",
        },
        {
            "name": "Отчетность",
            "document": "Дашборд руководителя",
            "state": "Готово",
            "check": "Остатки + превышения + статусы",
        },
        {
            "name": "Настройки",
            "document": "Заявки на интеграцию",
            "state": "В работе",
            "check": "Запрос администратору",
        },
    ]
    counts = {
        "articles": CashFlowArticle.objects.count(),
        "contracts": Contract.objects.count(),
        "payment_facts": PaymentFact.objects.count(),
        "sync_runs": SyncRun.objects.count(),
    }
    working_organization = _working_organization(request)
    workday = _build_workday_context(request.user, working_organization)
    return render(
        request,
        "core/workspace.html",
        {
            "modules": modules,
            "counts": counts,
            "workday": workday,
            "active_section": "workspace",
        },
    )


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST)
def nsi_dashboard(request):
    default_directory = next(iter(NSI_DIRECTORY_CONFIG))
    default_path = reverse("nsi_directory", kwargs={"directory": request.POST.get("directory") or default_directory})

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "sync":
                run = sync_one_c_dataset(MockOneCProvider())
                messages.success(request, f"Синхронизация Mock-1С завершена: {run.message}")
            elif action == "create":
                item = _create_nsi_item(request, path=default_path)
                messages.success(request, f"НСИ создана: {item}")
            else:
                raise ValueError("Неизвестное действие НСИ")
        except (IntegrityError, ValidationError, ValueError) as exc:
            messages.error(request, _format_nsi_error(exc))
        return redirect(default_path)

    return redirect(reverse("nsi_directory", kwargs={"directory": default_directory}))


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST)
def nsi_directory(request, directory: str):
    config = _nsi_directory_config(directory)
    model = config["model"]
    path = reverse("nsi_directory", kwargs={"directory": directory})

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "sync":
                run = sync_one_c_dataset(MockOneCProvider())
                messages.success(request, f"Синхронизация Mock-1С завершена: {run.message}")
            elif action == "create":
                item = _create_nsi_item(request, path=path)
                messages.success(request, f"Запись создана: {item}")
            elif action == "update":
                item = _update_nsi_item(request, path=path)
                messages.success(request, f"Запись обновлена: {item}")
            elif action == "delete":
                item_name = _delete_nsi_item(request, path=path)
                messages.success(request, f"Запись удалена: {item_name}")
            else:
                raise ValueError("Неизвестное действие НСИ")
        except (IntegrityError, ProtectedError, ValidationError, ValueError) as exc:
            messages.error(request, _format_nsi_error(exc))
        return redirect(path)

    objects = model.objects.order_by(*config["ordering"])
    context = {
        "latest_sync": SyncRun.objects.first(),
        "managed_directories": _nsi_directory_overview(),
        "directory": config,
        "directory_key": directory,
        "directory_rows": [_nsi_directory_object_row(obj, config) for obj in objects],
        "can_create_nsi": _has_model_permission(request.user, model, "add"),
        "can_update_nsi": _has_model_permission(request.user, model, "change"),
        "can_delete_nsi": _has_model_permission(request.user, model, "delete"),
        "active_section": "nsi",
    }
    return render(request, "core/nsi_directory.html", context)


NSI_DIRECTORY_CONFIG = {
    "organizations": {
        "model": Organization,
        "label": "Компании",
        "singular": "компанию",
        "description": "Юридические лица для бюджетов, лимитов, платежей и факта",
        "ordering": ["name"],
        "fields": [
            {"name": "name", "label": "Наименование", "required": True, "placeholder": 'ООО "Новая компания"'},
            {"name": "inn", "label": "ИНН", "maxlength": 12, "placeholder": "7700000000"},
        ],
    },
    "departments": {
        "model": Department,
        "label": "ЦФО",
        "singular": "ЦФО",
        "description": "Центры финансовой ответственности для бюджетов и лимитов",
        "ordering": ["code"],
        "fields": [
            {"name": "code", "label": "Код", "required": True, "transform": "upper", "placeholder": "CFO-003"},
            {"name": "name", "label": "Наименование", "required": True, "placeholder": "Проектный офис"},
        ],
    },
    "currencies": {
        "model": Currency,
        "label": "Валюты",
        "singular": "валюту",
        "description": "Валюты документов, договоров, бюджетов и лимитов",
        "ordering": ["code"],
        "fields": [
            {"name": "code", "label": "Код ISO", "required": True, "transform": "upper", "maxlength": 3, "placeholder": "KZT"},
            {"name": "name", "label": "Наименование", "required": True, "placeholder": "Казахстанский тенге"},
        ],
    },
    "articles": {
        "model": CashFlowArticle,
        "label": "Статьи ДДС",
        "singular": "статью ДДС",
        "description": "Классификатор движения денежных средств",
        "ordering": ["code"],
        "fields": [
            {"name": "code", "label": "Код", "required": True, "transform": "upper", "placeholder": "DDS-040"},
            {"name": "name", "label": "Наименование", "required": True, "placeholder": "Командировочные расходы"},
            {"name": "is_internal_turnover", "label": "ВГО", "kind": "checkbox"},
        ],
        "create_defaults": {"exists_in_one_c": False},
    },
    "counterparties": {
        "model": Counterparty,
        "label": "Контрагенты",
        "singular": "контрагента",
        "description": "Поставщики, покупатели и внутригрупповые компании",
        "ordering": ["name"],
        "fields": [
            {"name": "name", "label": "Наименование", "required": True, "placeholder": 'ООО "Поставщик"'},
            {"name": "inn", "label": "ИНН", "maxlength": 12, "placeholder": "7700000000"},
            {"name": "can_create_manually", "label": "Ручное создание", "kind": "checkbox", "default": True},
        ],
    },
    "nomenclature": {
        "model": Nomenclature,
        "label": "Номенклатура",
        "singular": "номенклатуру",
        "description": "Работы, услуги и позиции для договорного контура",
        "ordering": ["code"],
        "fields": [
            {"name": "code", "label": "Код", "required": True, "transform": "upper", "placeholder": "NOM-004"},
            {"name": "name", "label": "Наименование", "required": True, "placeholder": "Консультационные услуги"},
            {"name": "can_create_manually", "label": "Ручное создание", "kind": "checkbox", "default": True},
        ],
    },
}

NSI_DIRECTORY_MODELS = {key: config["model"] for key, config in NSI_DIRECTORY_CONFIG.items()}


def _nsi_directory_overview() -> list[dict]:
    return [_nsi_directory_row(key, config) for key, config in NSI_DIRECTORY_CONFIG.items()]


def _nsi_directory_row(key: str, config: dict) -> dict:
    model = config["model"]
    return {
        "key": key,
        "label": config["label"],
        "description": config["description"],
        "count": model.objects.count(),
        "manual_count": model.objects.filter(source_system=SourceSystem.MANUAL).count(),
        "external_count": model.objects.exclude(source_system=SourceSystem.MANUAL).count(),
    }


def _nsi_directory_config(directory: str) -> dict:
    config = NSI_DIRECTORY_CONFIG.get(directory)
    if config is None:
        raise Http404("Справочник НСИ не найден")
    return config


def _create_nsi_item(request, *, path: str):
    directory = request.POST.get("directory", "")
    config = _nsi_directory_config(directory)
    model = config["model"]
    if not _has_model_permission(request.user, model, "add"):
        raise ValueError("Недостаточно прав для создания элемента НСИ")
    values = _nsi_values_from_post(request, config)
    values.update(config.get("create_defaults", {}))
    item = model(**values, source_system=SourceSystem.MANUAL)
    item.full_clean()
    item.save()
    AuditLog.objects.create(
        user=request.user,
        action=AuditAction.CREATE,
        path=path,
        object_type=item.__class__.__name__,
        object_id=str(item.pk),
        message=f"Создан элемент НСИ {item}",
    )
    return item


def _update_nsi_item(request, *, path: str):
    directory = request.POST.get("directory", "")
    config = _nsi_directory_config(directory)
    model = config["model"]
    if not _has_model_permission(request.user, model, "change"):
        raise ValueError("Недостаточно прав для редактирования элемента НСИ")
    item = get_object_or_404(model, pk=request.POST.get("item_id"))
    values = _nsi_values_from_post(request, config)
    for field_name, value in values.items():
        setattr(item, field_name, value)
    item.full_clean()
    item.save()
    AuditLog.objects.create(
        user=request.user,
        action=AuditAction.UPDATE,
        path=path,
        object_type=item.__class__.__name__,
        object_id=str(item.pk),
        message=f"Обновлен элемент НСИ {item}",
    )
    return item


def _delete_nsi_item(request, *, path: str) -> str:
    directory = request.POST.get("directory", "")
    config = _nsi_directory_config(directory)
    model = config["model"]
    if not _has_model_permission(request.user, model, "delete"):
        raise ValueError("Недостаточно прав для удаления элемента НСИ")
    item = get_object_or_404(model, pk=request.POST.get("item_id"))
    item_name = str(item)
    object_id = str(item.pk)
    item.delete()
    AuditLog.objects.create(
        user=request.user,
        action=AuditAction.DELETE,
        path=path,
        object_type=model.__name__,
        object_id=object_id,
        message=f"Удален элемент НСИ {item_name}",
    )
    return item_name


def _nsi_values_from_post(request, config: dict) -> dict:
    values = {}
    for field in config["fields"]:
        field_name = field["name"]
        if field.get("kind") == "checkbox":
            values[field_name] = bool(request.POST.get(field_name))
            continue
        label = field["label"]
        raw_value = _required_post_value(request, field_name, label) if field.get("required") else (request.POST.get(field_name) or "").strip()
        if field.get("transform") == "upper":
            raw_value = raw_value.upper()
        values[field_name] = raw_value
    return values


def _nsi_directory_object_row(obj, config: dict) -> dict:
    return {
        "id": obj.pk,
        "object": obj,
        "source": obj.get_source_system_display(),
        "fields": [_nsi_directory_field_value(obj, field) for field in config["fields"]],
    }


def _nsi_directory_field_value(obj, field: dict) -> dict:
    value = getattr(obj, field["name"])
    field_data = {
        **field,
        "value": value,
        "input_value": "" if value is None else value,
    }
    if field.get("kind") == "checkbox":
        field_data["checked"] = bool(value)
    return field_data


def _required_post_value(request, field_name: str, label: str) -> str:
    value = (request.POST.get(field_name) or "").strip()
    if not value:
        raise ValueError(f"Заполните поле «{label}»")
    return value


def _format_nsi_error(exc) -> str:
    if isinstance(exc, ValidationError):
        if hasattr(exc, "messages"):
            return "; ".join(exc.messages)
        return "; ".join(f"{field}: {', '.join(messages)}" for field, messages in exc.message_dict.items())
    if isinstance(exc, ProtectedError):
        return "Запись уже используется в документах, поэтому ее нельзя удалить"
    if isinstance(exc, IntegrityError):
        return "Такой элемент НСИ уже существует или нарушает уникальность справочника"
    return str(exc)


def _can_create_nsi(user) -> bool:
    return any(_has_model_permission(user, model, "add") for model in NSI_DIRECTORY_MODELS.values())


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def planning_wizard(request):
    departments = list(Department.objects.all())
    working_organization = _working_organization(request)
    can_create_package = (
        _can_create_budgets(request.user)
        and _can_create_plans(request.user)
        and _can_edit_plans(request.user)
    )

    if request.method == "POST":
        try:
            if not can_create_package:
                raise ValueError(
                    "Первичный ввод бюджета и лимитов может выполнять экономист или администратор"
                )
            budget, limits = create_primary_budget_package(
                author=request.user,
                organization=working_organization,
                budget_year=int(request.POST.get("budget_year")),
                scope=request.POST.get("scope", ""),
                currency=get_object_or_404(Currency, pk=request.POST.get("currency_id")),
                approver=get_object_or_404(User, pk=request.POST.get("approver_id")),
                total_amount=_parse_decimal(request.POST.get("total_amount")),
                department_amounts=_parse_department_budget_amounts(request.POST, departments),
                limit_rows=_parse_primary_wizard_limit_rows(request.POST),
                comment=request.POST.get("comment", ""),
            )
            messages.success(
                request,
                f"Документ {budget.number} отправлен на утверждение. Лимитов в пакете: {len(limits)}",
            )
            return redirect("planning_budgets")
        except (InvalidOperation, TypeError, ValueError) as exc:
            messages.error(request, str(exc))
            return redirect("planning_wizard")

    context = {
        "active_section": "planning",
        "departments": departments,
        "articles": CashFlowArticle.objects.all(),
        "currencies": Currency.objects.all(),
        "approvers": User.objects.filter(profile__role=UserRole.MANAGER),
        "current_year": timezone.localdate().year,
        "working_organization": working_organization,
        "scope": BudgetScope,
        "periodicity": BudgetPeriodicity,
        "wizard_limit_rows": [
            {"index": index, "hidden": index > 3}
            for index in range(1, PRIMARY_WIZARD_LIMIT_ROWS + 1)
        ],
        "months": [{"number": number, "label": label} for number, label in WIZARD_MONTHS],
        "can_create_package": can_create_package,
    }
    return render(request, "core/planning_wizard.html", context)


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def planning_budgets(request):
    departments = list(Department.objects.all())
    working_organization = _working_organization(request)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "create":
                if not _can_create_budgets(request.user):
                    raise ValueError("Создавать бюджеты может только экономист или администратор")
                create_budget(
                    author=request.user,
                    organization=working_organization,
                    budget_year=int(request.POST.get("budget_year")),
                    scope=request.POST.get("scope", ""),
                    currency=get_object_or_404(Currency, pk=request.POST.get("currency_id")),
                    total_amount=_parse_decimal(request.POST.get("total_amount")),
                    department_amounts=_parse_department_budget_amounts(request.POST, departments),
                    comment=request.POST.get("comment", ""),
                )
                messages.success(request, "Бюджет создан")
            elif action == "approve":
                if not _can_approve_budgets(request.user):
                    raise ValueError("Утверждать бюджеты может руководитель или администратор")
                approve_budget(get_object_or_404(BudgetPlan, pk=request.POST.get("budget_id")), request.user)
                messages.success(request, "Бюджет утвержден")
        except (InvalidOperation, ValueError) as exc:
            messages.error(request, str(exc))
        return redirect("planning_budgets")

    rows = build_budget_rows(organization=working_organization)
    context = {
        "active_section": "planning",
        "rows": rows,
        "summary": build_budget_summary(rows),
        "departments": departments,
        "currencies": Currency.objects.all(),
        "current_year": timezone.localdate().year,
        "working_organization": working_organization,
        "scope": BudgetScope,
        "periodicity": BudgetPeriodicity,
        "status": BudgetPlanStatus,
        "can_create_budgets": _can_create_budgets(request.user),
        "can_approve_budgets": _can_approve_budgets(request.user),
    }
    return render(request, "core/planning_budgets.html", context)


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def planning_limits(request):
    working_organization = _working_organization(request)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "create":
                if not _can_create_plans(request.user):
                    raise ValueError("Создавать планы может только экономист или администратор")
                budget = None
                budget_id = request.POST.get("budget_id")
                if budget_id:
                    budget = get_object_or_404(BudgetPlan, pk=budget_id)
                create_budget_plan(
                    author=request.user,
                    budget=budget,
                    organization=working_organization,
                    department=get_object_or_404(Department, pk=request.POST.get("department_id")),
                    article=get_object_or_404(CashFlowArticle, pk=request.POST.get("article_id")),
                    currency=budget.currency if budget else get_object_or_404(Currency, pk=request.POST.get("currency_id")),
                    planning_year=budget.budget_year if budget else int(request.POST.get("planning_year")),
                    planning_horizon=int(request.POST.get("planning_horizon")),
                    annual_amount=_parse_decimal(request.POST.get("annual_amount")),
                    approver=get_object_or_404(User, pk=request.POST.get("approver_id")),
                    monthly_amounts=_parse_monthly_amounts(request.POST),
                    comment=request.POST.get("comment", ""),
                )
                messages.success(request, "План создан")
            elif action == "submit":
                if not _can_edit_plans(request.user):
                    raise ValueError("Отправлять планы может только экономист или администратор")
                submit_budget_plan(get_object_or_404(BudgetLimitPlan, pk=request.POST.get("plan_id")), request.user)
                messages.success(request, "План отправлен на утверждение")
            elif action == "approve":
                approve_budget_plan(get_object_or_404(BudgetLimitPlan, pk=request.POST.get("plan_id")), request.user)
                messages.success(request, "План утвержден")
            elif action == "create_adjustment":
                if not _has_model_permission(request.user, BudgetLimitAdjustment, "add"):
                    raise ValueError("Создавать корректировки может только экономист или администратор")
                create_limit_adjustment(
                    author=request.user,
                    base_plan=get_object_or_404(BudgetLimitPlan, pk=request.POST.get("base_plan_id")),
                    new_annual_amount=_parse_decimal(request.POST.get("new_annual_amount")),
                    reason=request.POST.get("reason", ""),
                    monthly_amounts=_parse_adjustment_monthly_amounts(request.POST),
                )
                messages.success(request, "Корректировка создана")
            elif action == "submit_adjustment":
                if not _has_model_permission(request.user, BudgetLimitAdjustment, "change"):
                    raise ValueError("Отправлять корректировки может только экономист или администратор")
                submit_limit_adjustment(
                    get_object_or_404(BudgetLimitAdjustment, pk=request.POST.get("adjustment_id")),
                    request.user,
                )
                messages.success(request, "Корректировка отправлена на утверждение")
            elif action == "approve_adjustment":
                approve_limit_adjustment(
                    get_object_or_404(BudgetLimitAdjustment, pk=request.POST.get("adjustment_id")),
                    request.user,
                )
                messages.success(request, "Корректировка утверждена")
        except (InvalidOperation, ValueError) as exc:
            messages.error(request, str(exc))
        return redirect("planning_limits")

    context = {
        "active_section": "planning",
        "plans": BudgetLimitPlan.objects.filter(organization=working_organization).select_related("budget", "department", "article", "currency", "approver", "author"),
        "adjustments": BudgetLimitAdjustment.objects.filter(base_plan__organization=working_organization).select_related("base_plan", "article", "approver", "author", "target_plan", "target_organization"),
        "approved_plans": BudgetLimitPlan.objects.filter(organization=working_organization, status=BudgetPlanStatus.APPROVED).select_related("currency"),
        "budgets": BudgetPlan.objects.filter(organization=working_organization, status=BudgetPlanStatus.APPROVED).select_related("currency"),
        "departments": Department.objects.all(),
        "articles": CashFlowArticle.objects.all(),
        "currencies": Currency.objects.all(),
        "approvers": User.objects.filter(profile__role=UserRole.MANAGER),
        "current_year": timezone.localdate().year,
        "working_organization": working_organization,
        "status": BudgetPlanStatus,
        "can_edit_plans": _can_edit_plans(request.user),
        "can_create_plans": _can_create_plans(request.user),
        "can_create_limit_adjustments": _can_create_limit_adjustments(request.user),
        "can_approve_plans": _can_approve_plans(request.user),
        "plans_acl": {
            "view": _has_model_permission(request.user, BudgetLimitPlan, "view"),
            "create": _can_create_plans(request.user),
            "edit": _can_edit_plans(request.user),
            "delete": _can_delete_plans(request.user),
        },
    }
    return render(request, "core/planning_limits.html", context)


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def limit_adjustment_wizard(request):
    can_create_adjustment = _can_create_limit_adjustments(request.user) and _can_edit_limit_adjustments(request.user)
    working_organization = _working_organization(request)

    if request.method == "POST":
        try:
            if not can_create_adjustment:
                raise ValueError("Заявку на корректировку лимита может создать экономист или администратор")
            adjustment = create_limit_adjustment_request(
                author=request.user,
                base_plan=get_object_or_404(
                    BudgetLimitPlan,
                    pk=request.POST.get("base_plan_id"),
                    status=BudgetPlanStatus.APPROVED,
                ),
                new_annual_amount=_parse_decimal(request.POST.get("new_annual_amount")),
                reason=request.POST.get("reason", ""),
                monthly_amounts=_parse_adjustment_monthly_amounts(request.POST),
                target_plan=_resolve_target_plan(request.POST.get("target_plan_id")),
            )
            messages.success(request, f"Заявка {adjustment.number} отправлена на утверждение")
            return redirect("planning_limits")
        except (InvalidOperation, TypeError, ValueError) as exc:
            messages.error(request, str(exc))
            return redirect("limit_adjustment_wizard")

    context = {
        "active_section": "planning",
        "approved_plans": BudgetLimitPlan.objects.filter(organization=working_organization, status=BudgetPlanStatus.APPROVED)
        .select_related("budget", "department", "article", "currency", "approver")
        .prefetch_related("months"),
        "target_plans": BudgetLimitPlan.objects.filter(status=BudgetPlanStatus.APPROVED)
        .exclude(organization=working_organization)
        .select_related("organization", "budget", "department", "article", "currency"),
        "months": [{"number": number, "label": label} for number, label in WIZARD_MONTHS],
        "can_create_adjustment": can_create_adjustment,
        "working_organization": working_organization,
    }
    return render(request, "core/limit_adjustment_wizard.html", context)


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def contracts_register(request):
    filters = ContractRegisterFilters(
        customer_contract_id=_parse_optional_int(request.GET.get("customer_contract_id")),
        counterparty_id=_parse_optional_int(request.GET.get("counterparty_id")),
        contract_query=(request.GET.get("contract") or "").strip().lower(),
        date_from=_parse_optional_date(request.GET.get("date_from")),
        date_to=_parse_optional_date(request.GET.get("date_to")),
    )
    rows, summary = build_contract_register(filters)
    counterparties = Counterparty.objects.filter(contracts__isnull=False).distinct().order_by("name")
    customer_contracts = (
        Contract.objects.filter(kind=ContractKind.CUSTOMER)
        .select_related("counterparty")
        .order_by("date", "number")
    )
    return render(
        request,
        "core/contracts_register.html",
        {
            "active_section": "contracts",
            "rows": rows,
            "summary": summary,
            "counterparties": counterparties,
            "customer_contracts": customer_contracts,
            "selected_customer_contract_id": filters.customer_contract_id,
            "selected_counterparty_id": filters.counterparty_id,
            "selected_contract": request.GET.get("contract", ""),
            "selected_date_from": request.GET.get("date_from", ""),
            "selected_date_to": request.GET.get("date_to", ""),
        },
    )


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def contracts_reservations(request):
    rows = build_contract_reservation_rows()
    summary = build_contract_reservation_summary(rows)
    return render(
        request,
        "core/contracts_reservations.html",
        {
            "active_section": "contracts",
            "rows": rows,
            "summary": summary,
        },
    )


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def contracts_tree(request):
    filters = ContractTreeFilters(
        counterparty_id=_parse_optional_int(request.GET.get("counterparty_id")),
        contract_query=(request.GET.get("contract") or "").strip().lower(),
        date_from=_parse_optional_date(request.GET.get("date_from")),
        date_to=_parse_optional_date(request.GET.get("date_to")),
    )
    rows, summary = build_contract_tree(filters)
    counterparties = Counterparty.objects.filter(contracts__isnull=False).distinct().order_by("name")
    return render(
        request,
        "core/contracts_tree.html",
        {
            "active_section": "contracts",
            "rows": rows,
            "summary": summary,
            "counterparties": counterparties,
            "selected_counterparty_id": filters.counterparty_id,
            "selected_contract": request.GET.get("contract", ""),
            "selected_date_from": request.GET.get("date_from", ""),
            "selected_date_to": request.GET.get("date_to", ""),
        },
    )


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def payment_requests(request):
    working_organization = _working_organization(request)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "create":
                if not _can_create_payment_requests(request.user):
                    raise ValueError("Создавать заявки может только экономист или администратор")
                contract = None
                contract_id = request.POST.get("contract_id")
                if contract_id:
                    contract = get_object_or_404(Contract, pk=contract_id)
                additional_agreement = None
                additional_agreement_id = request.POST.get("additional_agreement_id")
                if additional_agreement_id:
                    additional_agreement = get_object_or_404(AdditionalAgreement, pk=additional_agreement_id)
                counterparty = contract.counterparty if contract else get_object_or_404(
                    Counterparty,
                    pk=request.POST.get("counterparty_id"),
                )
                currency = contract.currency if contract else get_object_or_404(
                    Currency,
                    pk=request.POST.get("currency_id"),
                )
                amount_raw = (request.POST.get("amount") or "").strip()
                amount = _parse_decimal(amount_raw) if amount_raw else (contract.amount if contract else None)
                manual_exchange_rate_raw = (request.POST.get("manual_exchange_rate") or "").strip()
                manual_exchange_rate = (
                    _parse_decimal(manual_exchange_rate_raw)
                    if manual_exchange_rate_raw
                    else (contract.manual_exchange_rate if contract else None)
                )
                create_payment_request(
                    author=request.user,
                    request_kind=request.POST.get("request_kind"),
                    organization=get_object_or_404(Organization, pk=request.POST.get("organization_id")),
                    article=get_object_or_404(CashFlowArticle, pk=request.POST.get("article_id")),
                    counterparty=counterparty,
                    contract=contract,
                    additional_agreement=additional_agreement,
                    currency=currency,
                    amount=amount,
                    manual_exchange_rate=manual_exchange_rate,
                    approver=get_object_or_404(User, pk=request.POST.get("approver_id")),
                    invoice_number=request.POST.get("invoice_number", ""),
                    invoice_date=_parse_optional_date(request.POST.get("invoice_date")),
                    payment_purpose=request.POST.get("payment_purpose", ""),
                    comment=request.POST.get("comment", ""),
                    justification_text=request.POST.get("justification_text", ""),
                    justification_file=request.FILES.get("justification_file"),
                )
                messages.success(request, "Заявка создана")
            elif action == "submit":
                if not _can_manage_payment_requests(request.user):
                    raise ValueError("Отправлять заявки может только экономист или администратор")
                submit_payment_request(
                    get_object_or_404(PaymentRequest, pk=request.POST.get("request_id")),
                    request.user,
                )
                messages.success(request, "Заявка отправлена на согласование")
            elif action == "approve":
                if not _can_approve_payment_requests(request.user):
                    raise ValueError("Согласовывать заявки может только назначенный руководитель")
                approve_payment_request(
                    get_object_or_404(PaymentRequest, pk=request.POST.get("request_id")),
                    request.user,
                )
                messages.success(request, "Заявка согласована")
            elif action == "reject":
                if not _can_approve_payment_requests(request.user):
                    raise ValueError("Отклонять заявки может только назначенный руководитель")
                reject_payment_request(
                    get_object_or_404(PaymentRequest, pk=request.POST.get("request_id")),
                    request.user,
                    request.POST.get("approver_comment", ""),
                )
                messages.success(request, "Заявка отклонена")
            elif action == "transfer":
                if not _can_manage_payment_requests(request.user):
                    raise ValueError("Передавать в 1С:ДО может только экономист или администратор")
                transfer_payment_request_to_do(
                    get_object_or_404(PaymentRequest, pk=request.POST.get("request_id")),
                    request.user,
                )
                messages.success(request, "Заявка передана в 1С:ДО")
        except (InvalidOperation, ValueError) as exc:
            messages.error(request, str(exc))
        return redirect("payment_requests")

    contracts = list(
        Contract.objects.filter(kind=ContractKind.SOLE_SUPPLIER).select_related("counterparty", "currency")
    )
    agreements = list(
        AdditionalAgreement.objects.select_related("contract", "currency", "contract__counterparty").order_by("date", "number")
    )
    limit_settings = PaymentRequestControlSettings.active_or_default()
    context = {
        "active_section": "payments",
        "requests": PaymentRequest.objects.select_related(
            "organization",
            "article",
            "counterparty",
            "contract",
            "additional_agreement",
            "currency",
            "approver",
            "author",
        ).filter(organization=working_organization),
        "organizations": Organization.objects.all(),
        "working_organization": working_organization,
        "articles": CashFlowArticle.objects.all(),
        "counterparties": Counterparty.objects.all(),
        "contracts": contracts,
        "agreements": agreements,
        "contracts_autofill": [
            {
                "id": contract.id,
                "counterparty_id": contract.counterparty_id,
                "currency_id": contract.currency_id,
                "amount": str(contract.amount),
                "manual_exchange_rate": str(contract.manual_exchange_rate),
            }
            for contract in contracts
        ],
        "agreements_autofill": [
            {
                "id": agreement.id,
                "contract_id": agreement.contract_id,
            }
            for agreement in agreements
        ],
        "currencies": Currency.objects.all(),
        "approvers": User.objects.filter(profile__role=UserRole.MANAGER),
        "status": PaymentRequestStatus,
        "request_kind": PaymentRequestKind,
        "can_create_payment_requests": _can_create_payment_requests(request.user),
        "can_manage_payment_requests": _can_manage_payment_requests(request.user),
        "can_approve_payment_requests": _can_approve_payment_requests(request.user),
        "payments_acl": {
            "view": _has_model_permission(request.user, PaymentRequest, "view"),
            "create": _can_create_payment_requests(request.user),
            "edit": _can_manage_payment_requests(request.user),
            "delete": _can_delete_payment_requests(request.user),
        },
        "limit_control_mode": limit_settings.control_mode,
        "limit_control_mode_label": (
            "Блокировка" if limit_settings.control_mode == PaymentLimitControlMode.BLOCK else "Предупреждение"
        ),
    }
    return render(request, "core/payment_requests.html", context)


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def payment_request_wizard(request):
    can_create_request = _can_create_payment_requests(request.user) and _can_manage_payment_requests(request.user)
    working_organization = _working_organization(request)
    if request.method == "POST":
        try:
            if not can_create_request:
                raise ValueError("Заявку на оплату может создать экономист или администратор")
            payment_request = _create_payment_request_from_request(request)
            submit_payment_request(payment_request, request.user)
            messages.success(request, f"Заявка {payment_request.number} отправлена на согласование")
            return redirect("payment_requests")
        except (InvalidOperation, ValueError) as exc:
            messages.error(request, str(exc))
            return redirect("payment_request_wizard")

    context = {
        "active_section": "payments",
        "request_kind": PaymentRequestKind,
        "can_create_request": can_create_request,
        "working_organization": working_organization,
        **_payment_request_reference_context(),
    }
    return render(request, "core/payment_request_wizard.html", context)


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def payment_request_edit_wizard(request, request_id: int):
    payment_request = get_object_or_404(PaymentRequest, pk=request_id)
    can_edit_request = _can_manage_payment_requests(request.user) and payment_request.status in [
        PaymentRequestStatus.DRAFT,
        PaymentRequestStatus.REJECTED,
    ]

    if request.method == "POST":
        try:
            if not can_edit_request:
                raise ValueError("Редактировать заявку может экономист или администратор")
            _update_payment_request_from_request(payment_request, request)
            messages.success(request, f"Заявка {payment_request.number} сохранена")
            return redirect("payment_requests")
        except (InvalidOperation, ValueError) as exc:
            messages.error(request, str(exc))
            return redirect("payment_request_edit_wizard", request_id=payment_request.id)

    context = {
        "active_section": "payments",
        "payment_request": payment_request,
        "request_kind": PaymentRequestKind,
        "can_edit_request": can_edit_request,
        "working_organization": _working_organization(request),
        **_payment_request_reference_context(),
    }
    return render(request, "core/payment_request_edit_wizard.html", context)


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def payment_facts(request):
    working_organization = _working_organization(request)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "adjust":
                if not _can_edit_payment_facts(request.user):
                    raise ValueError("Корректировать факт может только экономист или администратор")
                fact = get_object_or_404(PaymentFact, pk=request.POST.get("fact_id"))
                adjust_payment_fact(
                    payment_fact=fact,
                    author=request.user,
                    new_amount=_parse_decimal(request.POST.get("new_amount")),
                    reason=request.POST.get("reason", ""),
                    new_comment=request.POST.get("new_comment", ""),
                )
                messages.success(request, "Корректировка факта сохранена")
        except (InvalidOperation, ValueError) as exc:
            messages.error(request, str(exc))
        return redirect("payment_facts")

    current_year = timezone.localdate().year
    selected_year = _parse_optional_int(request.GET.get("year")) or current_year
    selected_month = _parse_optional_int(request.GET.get("month"))
    selected_accounting_kind = (request.GET.get("accounting_kind") or "").strip()
    if selected_accounting_kind not in ("", "bu", "nu"):
        selected_accounting_kind = ""
    selected_article_id = _parse_optional_int(request.GET.get("article_id"))
    selected_counterparty_id = _parse_optional_int(request.GET.get("counterparty_id"))

    facts_query = PaymentFact.objects.select_related(
        "organization", "article", "counterparty", "contract", "currency"
    ).filter(organization=working_organization, date__year=selected_year)
    if selected_month:
        facts_query = facts_query.filter(date__month=selected_month)
    if selected_accounting_kind:
        facts_query = facts_query.filter(accounting_kind=selected_accounting_kind)
    if selected_article_id:
        facts_query = facts_query.filter(article_id=selected_article_id)
    if selected_counterparty_id:
        facts_query = facts_query.filter(counterparty_id=selected_counterparty_id)

    facts = list(facts_query.order_by("-date", "-id"))
    fact_ids = [fact.id for fact in facts]
    adjustments_stats = {
        row["payment_fact_id"]: row
        for row in PaymentFactAdjustment.objects.filter(payment_fact_id__in=fact_ids)
        .values("payment_fact_id")
        .annotate(versions=Count("id"), latest_version=Max("version"), latest_at=Max("created_at"))
    }

    rows = []
    for fact in facts:
        stat = adjustments_stats.get(fact.id, {})
        rows.append(
            {
                "fact": fact,
                "versions": stat.get("versions", 0),
                "latest_version": stat.get("latest_version", 0),
                "latest_at": stat.get("latest_at"),
            }
        )

    context = {
        "active_section": "payments",
        "rows": rows,
        "current_year": current_year,
        "years": list(range(current_year - 1, current_year + 3)),
        "months": list(range(1, 13)),
        "articles": CashFlowArticle.objects.order_by("code"),
        "counterparties": Counterparty.objects.order_by("name"),
        "selected_year": selected_year,
        "selected_month": selected_month,
        "selected_accounting_kind": selected_accounting_kind,
        "selected_article_id": selected_article_id,
        "selected_counterparty_id": selected_counterparty_id,
        "working_organization": working_organization,
        "can_view_payment_facts": _can_view_payment_facts(request.user),
        "can_edit_payment_facts": _can_edit_payment_facts(request.user),
        "payment_facts_acl": {
            "view": _can_view_payment_facts(request.user),
            "create": _can_create_payment_facts(request.user),
            "edit": _can_edit_payment_facts(request.user),
            "delete": _can_delete_payment_facts(request.user),
        },
    }
    return render(request, "core/payment_facts.html", context)


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def payment_fact_adjustment_wizard(request):
    if request.method == "POST":
        try:
            if not _can_edit_payment_facts(request.user):
                raise ValueError("Корректировать факт может только экономист или администратор")
            fact = get_object_or_404(PaymentFact, pk=request.POST.get("fact_id"))
            adjustment = adjust_payment_fact(
                payment_fact=fact,
                author=request.user,
                new_amount=_parse_decimal(request.POST.get("new_amount")),
                reason=request.POST.get("reason", ""),
                new_comment=request.POST.get("new_comment", ""),
            )
            messages.success(request, f"Корректировка факта v{adjustment.version} сохранена")
            return redirect("payment_facts")
        except (InvalidOperation, ValueError) as exc:
            messages.error(request, str(exc))
            return redirect("payment_fact_adjustment_wizard")

    facts = PaymentFact.objects.select_related(
        "organization",
        "article",
        "counterparty",
        "contract",
        "currency",
    ).order_by("-date", "-id")[:200]
    context = {
        "active_section": "payments",
        "facts": facts,
        "selected_fact_id": _parse_optional_int(request.GET.get("fact_id")),
        "can_edit_payment_facts": _can_edit_payment_facts(request.user),
    }
    return render(request, "core/payment_fact_adjustment_wizard.html", context)


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def _legacy_plan_fact_report(request):
    current_year = timezone.localdate().year
    selected_year = _parse_optional_int(request.GET.get("year")) or current_year
    selected_status = (request.GET.get("request_status") or "").strip()
    if selected_status not in PaymentRequestStatus.values:
        selected_status = ""

    filters = PlanFactFilters(
        year=selected_year,
        article_id=_parse_optional_int(request.GET.get("article_id")),
        counterparty_id=_parse_optional_int(request.GET.get("counterparty_id")),
        request_status=selected_status,
    )
    rows, summary = build_plan_fact_report(filters)

    if request.GET.get("export") == "excel":
        response = HttpResponse(plan_fact_to_csv(rows), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="plan_fact_{selected_year}.csv"'
        return response

    context = {
        "active_section": "reports",
        "rows": rows,
        "summary": summary,
        "years": list(range(current_year - 1, current_year + 3)),
        "articles": CashFlowArticle.objects.order_by("code"),
        "counterparties": Counterparty.objects.order_by("name"),
        "request_status_choices": [("", "Все статусы")] + list(PaymentRequestStatus.choices),
        "selected_year": selected_year,
        "selected_article_id": filters.article_id,
        "selected_counterparty_id": filters.counterparty_id,
        "selected_request_status": selected_status,
    }
    return render(request, "core/plan_fact_report.html", context)


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def plan_fact_report(request):
    working_organization = _working_organization(request)
    current_year = timezone.localdate().year
    templates = list(
        ReportTemplate.objects.filter(owner=request.user, report_type=ReportTemplateType.PLAN_FACT).order_by("name")
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

    selected_template_id = _parse_optional_int(request.GET.get("template_id"))
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
        request.GET,
        base_filters,
        "request_status",
        allowed_values=PaymentRequestStatus.values,
    )
    selected_article_id = _read_filter_int(request.GET, base_filters, "article_id")
    selected_counterparty_id = _read_filter_int(request.GET, base_filters, "counterparty_id")
    selected_organization_id = _read_filter_int(request.GET, base_filters, "organization_id") or working_organization.id
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

    context = {
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
        "working_organization": working_organization,
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
    }
    return render(request, "core/plan_fact_report.html", context)


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def manager_dashboard(request):
    working_organization = _working_organization(request)
    current_year = timezone.localdate().year
    selected_year = _parse_optional_int(request.GET.get("year")) or current_year
    payload = build_manager_dashboard(year=selected_year, organization_id=working_organization.id)
    context = {
        "active_section": "reports",
        "years": list(range(current_year - 1, current_year + 3)),
        "selected_year": selected_year,
        "working_organization": working_organization,
        **payload,
    }
    return render(request, "core/manager_dashboard.html", context)


@external_accounting_required
def external_accounting(request):
    if request.method == "POST":
        contract = get_object_or_404(
            Contract,
            pk=request.POST.get("contract_id"),
            kind=ContractKind.SOLE_SUPPLIER,
        )
        try:
            amount = _parse_decimal(request.POST.get("amount")) or remaining_contract_amount(contract)
            payment = pay_contract(contract=contract, accountant=request.user, amount=amount)
            messages.success(request, f"Оплата {payment.number} проведена")
        except (InvalidOperation, ValueError) as exc:
            messages.error(request, str(exc))
        return redirect("external_accounting")

    supplier_contracts = Contract.objects.filter(kind=ContractKind.SOLE_SUPPLIER).select_related(
        "counterparty",
        "currency",
    )
    rows = []
    for contract in supplier_contracts:
        paid_amount = paid_amount_for_contract(contract)
        remaining_amount = remaining_contract_amount(contract)
        rows.append(
            {
                "contract": contract,
                "reserved_amount": contract.reserved_amount,
                "paid_amount": paid_amount,
                "remaining_amount": remaining_amount,
            }
        )

    return render(
        request,
        "core/external_accounting.html",
        {
            "active_section": "external_accounting",
            "rows": rows,
            "payments_total": sum(row["paid_amount"] for row in rows),
        },
    )


@external_accounting_required
def external_payment_wizard(request):
    if request.method == "POST":
        contract = get_object_or_404(
            Contract,
            pk=request.POST.get("contract_id"),
            kind=ContractKind.SOLE_SUPPLIER,
        )
        try:
            amount = _parse_decimal(request.POST.get("amount")) or remaining_contract_amount(contract)
            payment = pay_contract(contract=contract, accountant=request.user, amount=amount)
            messages.success(request, f"Оплата {payment.number} проведена")
            return redirect("external_accounting")
        except (InvalidOperation, ValueError) as exc:
            messages.error(request, str(exc))
            return redirect("external_payment_wizard")

    supplier_contracts = Contract.objects.filter(kind=ContractKind.SOLE_SUPPLIER).select_related(
        "counterparty",
        "currency",
    )
    rows = []
    for contract in supplier_contracts:
        paid_amount = paid_amount_for_contract(contract)
        remaining_amount = remaining_contract_amount(contract)
        if remaining_amount > 0:
            rows.append(
                {
                    "contract": contract,
                    "paid_amount": paid_amount,
                    "remaining_amount": remaining_amount,
                }
            )
    return render(
        request,
        "core/external_payment_wizard.html",
        {
            "active_section": "external_accounting",
            "rows": rows,
        },
    )


@login_required
def instruction(request):
    guide_path = USER_GUIDE_DIR / "instruction.md"
    try:
        markdown_text = guide_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Http404("Руководство не найдено") from exc

    context = {
        "active_section": "instruction",
        "guide_html": mark_safe(_render_user_guide_markdown(markdown_text)),
    }
    return render(request, "core/instruction.html", context)


@login_required
def instruction_asset(request, asset_path: str):
    asset_root = USER_GUIDE_DIR.resolve()
    requested_path = Path(asset_path)
    if requested_path.is_absolute() or ".." in requested_path.parts:
        raise Http404("Некорректный путь")

    resolved_path = (USER_GUIDE_DIR / requested_path).resolve()
    if asset_root not in resolved_path.parents:
        raise Http404("Файл не найден")
    if not resolved_path.is_file():
        raise Http404("Файл не найден")

    allowed_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    if resolved_path.suffix.lower() not in allowed_suffixes:
        raise Http404("Тип файла не поддерживается")

    content_type = guess_type(str(resolved_path))[0] or "application/octet-stream"
    return FileResponse(resolved_path.open("rb"), content_type=content_type)


def healthz(request):
    database = "ok"
    try:
        connection.ensure_connection()
    except Exception:
        database = "error"

    status = 200 if database == "ok" else 503
    return JsonResponse({"status": "ok" if status == 200 else "error", "database": database}, status=status)


def _parse_decimal(raw_value):
    if not raw_value:
        return None
    return Decimal(raw_value.replace(" ", "").replace(",", "."))


def _payment_request_reference_context():
    contracts = list(
        Contract.objects.filter(kind=ContractKind.SOLE_SUPPLIER).select_related("counterparty", "currency")
    )
    agreements = list(
        AdditionalAgreement.objects.select_related("contract", "currency", "contract__counterparty").order_by(
            "date",
            "number",
        )
    )
    return {
        "organizations": Organization.objects.all(),
        "articles": CashFlowArticle.objects.all(),
        "counterparties": Counterparty.objects.all(),
        "contracts": contracts,
        "agreements": agreements,
        "contracts_autofill": [
            {
                "id": contract.id,
                "counterparty_id": contract.counterparty_id,
                "currency_id": contract.currency_id,
                "amount": str(contract.amount),
                "manual_exchange_rate": str(contract.manual_exchange_rate),
            }
            for contract in contracts
        ],
        "agreements_autofill": [
            {
                "id": agreement.id,
                "contract_id": agreement.contract_id,
            }
            for agreement in agreements
        ],
        "currencies": Currency.objects.all(),
        "approvers": User.objects.filter(profile__role=UserRole.MANAGER),
    }


def _create_payment_request_from_request(request):
    contract = None
    contract_id = request.POST.get("contract_id")
    if contract_id:
        contract = get_object_or_404(Contract, pk=contract_id)

    additional_agreement = None
    additional_agreement_id = request.POST.get("additional_agreement_id")
    if additional_agreement_id:
        additional_agreement = get_object_or_404(AdditionalAgreement, pk=additional_agreement_id)

    counterparty = contract.counterparty if contract else get_object_or_404(
        Counterparty,
        pk=request.POST.get("counterparty_id"),
    )
    currency = contract.currency if contract else get_object_or_404(
        Currency,
        pk=request.POST.get("currency_id"),
    )
    amount_raw = (request.POST.get("amount") or "").strip()
    amount = _parse_decimal(amount_raw) if amount_raw else (contract.amount if contract else None)
    manual_exchange_rate_raw = (request.POST.get("manual_exchange_rate") or "").strip()
    manual_exchange_rate = (
        _parse_decimal(manual_exchange_rate_raw)
        if manual_exchange_rate_raw
        else (contract.manual_exchange_rate if contract else None)
    )
    return create_payment_request(
        author=request.user,
        request_kind=request.POST.get("request_kind"),
        organization=get_object_or_404(Organization, pk=request.POST.get("organization_id")),
        article=get_object_or_404(CashFlowArticle, pk=request.POST.get("article_id")),
        counterparty=counterparty,
        contract=contract,
        additional_agreement=additional_agreement,
        currency=currency,
        amount=amount,
        manual_exchange_rate=manual_exchange_rate,
        approver=get_object_or_404(User, pk=request.POST.get("approver_id")),
        invoice_number=request.POST.get("invoice_number", ""),
        invoice_date=_parse_optional_date(request.POST.get("invoice_date")),
        payment_purpose=request.POST.get("payment_purpose", ""),
        comment=request.POST.get("comment", ""),
        justification_text=request.POST.get("justification_text", ""),
        justification_file=request.FILES.get("justification_file"),
    )


def _update_payment_request_from_request(payment_request, request):
    contract = None
    contract_id = request.POST.get("contract_id")
    if contract_id:
        contract = get_object_or_404(Contract, pk=contract_id)

    additional_agreement = None
    additional_agreement_id = request.POST.get("additional_agreement_id")
    if additional_agreement_id:
        additional_agreement = get_object_or_404(AdditionalAgreement, pk=additional_agreement_id)

    counterparty = contract.counterparty if contract else get_object_or_404(
        Counterparty,
        pk=request.POST.get("counterparty_id"),
    )
    currency = contract.currency if contract else get_object_or_404(
        Currency,
        pk=request.POST.get("currency_id"),
    )
    amount_raw = (request.POST.get("amount") or "").strip()
    amount = _parse_decimal(amount_raw) if amount_raw else (contract.amount if contract else None)
    manual_exchange_rate_raw = (request.POST.get("manual_exchange_rate") or "").strip()
    manual_exchange_rate = (
        _parse_decimal(manual_exchange_rate_raw)
        if manual_exchange_rate_raw
        else (contract.manual_exchange_rate if contract else None)
    )
    return update_payment_request(
        request=payment_request,
        user=request.user,
        request_kind=request.POST.get("request_kind"),
        organization=get_object_or_404(Organization, pk=request.POST.get("organization_id")),
        article=get_object_or_404(CashFlowArticle, pk=request.POST.get("article_id")),
        counterparty=counterparty,
        contract=contract,
        additional_agreement=additional_agreement,
        currency=currency,
        amount=amount,
        manual_exchange_rate=manual_exchange_rate,
        approver=get_object_or_404(User, pk=request.POST.get("approver_id")),
        invoice_number=request.POST.get("invoice_number", ""),
        invoice_date=_parse_optional_date(request.POST.get("invoice_date")),
        payment_purpose=request.POST.get("payment_purpose", ""),
        comment=request.POST.get("comment", ""),
        justification_text=request.POST.get("justification_text", ""),
        justification_file=request.FILES.get("justification_file"),
    )


def _parse_monthly_amounts(post_data):
    values = {}
    seen_any = False
    for month in range(1, 13):
        raw_value = post_data.get(f"month_{month}")
        if raw_value in (None, ""):
            continue
        seen_any = True
        values[month] = _parse_decimal(raw_value)
    if not seen_any:
        return None
    if set(values.keys()) != set(range(1, 13)):
        raise ValueError("Если указываете помесячную разбивку, заполните все 12 месяцев")
    return values


def _parse_adjustment_monthly_amounts(post_data):
    values = {}
    seen_any = False
    for month in range(1, 13):
        raw_value = post_data.get(f"adjustment_month_{month}")
        if raw_value in (None, ""):
            continue
        seen_any = True
        values[month] = _parse_decimal(raw_value)
    if not seen_any:
        return None
    if set(values.keys()) != set(range(1, 13)):
        raise ValueError("Если указываете помесячную корректировку, заполните все 12 месяцев")
    return values


def _resolve_target_plan(raw_plan_id):
    plan_id = _parse_optional_int(raw_plan_id)
    if not plan_id:
        return None
    return get_object_or_404(BudgetLimitPlan, pk=plan_id, status=BudgetPlanStatus.APPROVED)


def _parse_department_budget_amounts(post_data, departments):
    values = {}
    for department in departments:
        raw_value = (post_data.get(f"department_budget_{department.id}") or "").strip()
        if not raw_value:
            continue
        values[department.id] = _parse_decimal(raw_value)
    return values


def _parse_primary_wizard_limit_rows(post_data):
    rows = []
    for index in range(1, PRIMARY_WIZARD_LIMIT_ROWS + 1):
        department_id = (post_data.get(f"limit_{index}_department_id") or "").strip()
        article_id = (post_data.get(f"limit_{index}_article_id") or "").strip()
        annual_amount_raw = (post_data.get(f"limit_{index}_annual_amount") or "").strip()
        has_months = any(
            (post_data.get(f"limit_{index}_month_{month}") or "").strip()
            for month, _label in WIZARD_MONTHS
        )

        if not any([department_id, article_id, annual_amount_raw, has_months]):
            continue
        if not department_id or not article_id or not annual_amount_raw:
            raise ValueError(f"Заполните ЦФО, статью и годовой лимит в строке {index}")

        rows.append(
            {
                "department": get_object_or_404(Department, pk=department_id),
                "article": get_object_or_404(CashFlowArticle, pk=article_id),
                "annual_amount": _parse_decimal(annual_amount_raw),
                "monthly_amounts": _parse_primary_wizard_monthly_amounts(post_data, index),
                "comment": (post_data.get(f"limit_{index}_comment") or "").strip(),
            }
        )

    if not rows:
        raise ValueError("Добавьте минимум один лимит")
    return rows


def _parse_primary_wizard_monthly_amounts(post_data, index: int):
    values = {}
    seen_any = False
    for month, _label in WIZARD_MONTHS:
        raw_value = (post_data.get(f"limit_{index}_month_{month}") or "").strip()
        if not raw_value:
            continue
        seen_any = True
        values[month] = _parse_decimal(raw_value)
    if not seen_any:
        return None
    if set(values.keys()) != {month for month, _label in WIZARD_MONTHS}:
        raise ValueError(
            f"Если указываете помесячную разбивку в строке {index}, заполните все 12 месяцев"
        )
    return values


def _parse_optional_int(raw_value):
    if not raw_value:
        return None
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


def _parse_optional_date(raw_value: str | None) -> date | None:
    if not raw_value:
        return None
    try:
        return date.fromisoformat(raw_value)
    except ValueError:
        pass
    try:
        return datetime.strptime(raw_value, "%d.%m.%Y").date()
    except ValueError:
        return None


def _resolve_report_template(templates_by_id: dict[int, ReportTemplate], raw_template_id) -> ReportTemplate:
    template_id = _parse_optional_int(raw_template_id)
    if not template_id:
        raise ValueError("Выберите шаблон отчета")
    template = templates_by_id.get(template_id)
    if template is None:
        raise ValueError("Шаблон отчета не найден")
    return template


def _read_filter_int(query_params, base_filters: dict, key: str) -> int | None:
    if key in query_params:
        return _parse_optional_int(query_params.get(key))
    return _parse_optional_int(base_filters.get(key))


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
    year = _parse_optional_int(data.get("year")) or default_year
    request_status = (data.get("request_status") or "").strip()
    if request_status not in PaymentRequestStatus.values:
        request_status = ""
    return {
        "year": year,
        "article_id": _parse_optional_int(data.get("article_id")),
        "counterparty_id": _parse_optional_int(data.get("counterparty_id")),
        "organization_id": _parse_optional_int(data.get("organization_id")),
        "customer_contract_id": _parse_optional_int(data.get("customer_contract_id")),
        "supplier_contract_id": _parse_optional_int(data.get("supplier_contract_id")),
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


def _document_action_config(document_type: str, action: str, document_id: int):
    def payment_document():
        return get_object_or_404(
            PaymentRequest.objects.select_related(
                "organization",
                "article",
                "counterparty",
                "contract",
                "additional_agreement",
                "currency",
                "approver",
                "author",
            ),
            pk=document_id,
        )

    configs = {
        ("budget", "submit"): {
            "document": lambda: get_object_or_404(BudgetPlan.objects.select_related("organization", "currency", "author"), pk=document_id),
            "active_section": "planning",
            "return_route": "planning_budgets",
            "document_kind": "Бюджет",
            "action_label": "Отправка бюджета",
            "action_text": "Бюджет будет отправлен на утверждение и останется доступен в журнале бюджетов.",
            "submit_label": "Отправить на утверждение",
            "permission_error": "Отправлять бюджет может экономист или администратор",
            "can_execute": lambda user, document: _can_create_budgets(user),
            "execute": lambda document, user, comment: submit_budget(document, user),
            "success_message": "Бюджет {number} отправлен на утверждение",
            "details": _budget_action_details,
        },
        ("budget", "approve"): {
            "document": lambda: get_object_or_404(BudgetPlan.objects.select_related("organization", "currency", "author"), pk=document_id),
            "active_section": "planning",
            "return_route": "planning_budgets",
            "document_kind": "Бюджет",
            "action_label": "Утверждение бюджета",
            "action_text": "После утверждения бюджет станет базой контроля лимитов.",
            "submit_label": "Утвердить бюджет",
            "permission_error": "Утверждать бюджеты может руководитель или администратор",
            "can_execute": lambda user, document: _can_approve_budgets(user),
            "execute": lambda document, user, comment: approve_budget(document, user),
            "success_message": "Бюджет {number} утвержден",
            "details": _budget_action_details,
        },
        ("limit", "submit"): {
            "document": lambda: get_object_or_404(
                BudgetLimitPlan.objects.select_related("organization", "budget", "department", "article", "currency", "approver", "author"),
                pk=document_id,
            ),
            "active_section": "planning",
            "return_route": "planning_limits",
            "document_kind": "Лимит",
            "action_label": "Отправка лимита",
            "action_text": "Лимит будет передан назначенному руководителю на утверждение.",
            "submit_label": "Отправить на утверждение",
            "permission_error": "Отправлять лимиты может экономист или администратор",
            "can_execute": lambda user, document: _can_edit_plans(user),
            "execute": lambda document, user, comment: submit_budget_plan(document, user),
            "success_message": "Лимит {number} отправлен на утверждение",
            "details": _limit_action_details,
        },
        ("limit", "approve"): {
            "document": lambda: get_object_or_404(
                BudgetLimitPlan.objects.select_related("organization", "budget", "department", "article", "currency", "approver", "author"),
                pk=document_id,
            ),
            "active_section": "planning",
            "return_route": "planning_limits",
            "document_kind": "Лимит",
            "action_label": "Утверждение лимита",
            "action_text": "После утверждения лимит участвует в контроле платежных заявок.",
            "submit_label": "Утвердить лимит",
            "permission_error": "Утверждать лимиты может назначенный руководитель или администратор",
            "can_execute": lambda user, document: _can_approve_plans(user),
            "execute": lambda document, user, comment: approve_budget_plan(document, user),
            "success_message": "Лимит {number} утвержден",
            "details": _limit_action_details,
        },
        ("limit-adjustment", "submit"): {
            "document": lambda: get_object_or_404(
                BudgetLimitAdjustment.objects.select_related(
                    "base_plan",
                    "base_plan__organization",
                    "base_plan__department",
                    "base_plan__currency",
                    "target_plan",
                    "target_organization",
                    "article",
                    "approver",
                    "author",
                ),
                pk=document_id,
            ),
            "active_section": "planning",
            "return_route": "planning_limits",
            "document_kind": "Корректировка лимита",
            "action_label": "Отправка корректировки",
            "action_text": "Заявка на корректировку лимита будет передана руководителю.",
            "submit_label": "Отправить на утверждение",
            "permission_error": "Отправлять корректировки может экономист или администратор",
            "can_execute": lambda user, document: _can_edit_limit_adjustments(user),
            "execute": lambda document, user, comment: submit_limit_adjustment(document, user),
            "success_message": "Корректировка {number} отправлена на утверждение",
            "details": _limit_adjustment_action_details,
        },
        ("limit-adjustment", "approve"): {
            "document": lambda: get_object_or_404(
                BudgetLimitAdjustment.objects.select_related(
                    "base_plan",
                    "base_plan__organization",
                    "base_plan__department",
                    "base_plan__currency",
                    "target_plan",
                    "target_organization",
                    "article",
                    "approver",
                    "author",
                ),
                pk=document_id,
            ),
            "active_section": "planning",
            "return_route": "planning_limits",
            "document_kind": "Корректировка лимита",
            "action_label": "Утверждение корректировки",
            "action_text": "После утверждения новая сумма и месячная разбивка обновят базовый лимит.",
            "submit_label": "Утвердить корректировку",
            "permission_error": "Утверждать корректировку может назначенный руководитель или администратор",
            "can_execute": lambda user, document: _can_approve_plans(user),
            "execute": lambda document, user, comment: approve_limit_adjustment(document, user),
            "success_message": "Корректировка {number} утверждена",
            "details": _limit_adjustment_action_details,
        },
        ("payment-request", "submit"): {
            "document": payment_document,
            "active_section": "payments",
            "return_route": "payment_requests",
            "document_kind": "Заявка на оплату",
            "action_label": "Отправка заявки",
            "action_text": "Заявка будет отправлена назначенному руководителю на согласование.",
            "submit_label": "Отправить на согласование",
            "permission_error": "Отправлять заявки может экономист или администратор",
            "can_execute": lambda user, document: _can_manage_payment_requests(user),
            "execute": lambda document, user, comment: submit_payment_request(document, user),
            "success_message": "Заявка {number} отправлена на согласование",
            "details": _payment_request_action_details,
        },
        ("payment-request", "approve"): {
            "document": payment_document,
            "active_section": "payments",
            "return_route": "payment_requests",
            "document_kind": "Заявка на оплату",
            "action_label": "Согласование заявки",
            "action_text": "После согласования заявка будет готова к передаче во внешний документооборот.",
            "submit_label": "Согласовать заявку",
            "permission_error": "Согласовывать заявки может назначенный руководитель",
            "can_execute": lambda user, document: _can_approve_payment_requests(user),
            "execute": lambda document, user, comment: approve_payment_request(document, user),
            "success_message": "Заявка {number} согласована",
            "details": _payment_request_action_details,
        },
        ("payment-request", "reject"): {
            "document": payment_document,
            "active_section": "payments",
            "return_route": "payment_requests",
            "document_kind": "Заявка на оплату",
            "action_label": "Отклонение заявки",
            "action_text": "Заявка вернется автору на исправление. Комментарий обязателен.",
            "submit_label": "Отклонить заявку",
            "permission_error": "Отклонять заявки может назначенный руководитель",
            "can_execute": lambda user, document: _can_approve_payment_requests(user),
            "execute": lambda document, user, comment: reject_payment_request(document, user, comment),
            "success_message": "Заявка {number} отклонена",
            "comment_required": True,
            "comment_label": "Причина отклонения",
            "comment_placeholder": "Что нужно исправить в заявке",
            "details": _payment_request_action_details,
        },
        ("payment-request", "transfer"): {
            "document": payment_document,
            "active_section": "payments",
            "return_route": "payment_requests",
            "document_kind": "Заявка на оплату",
            "action_label": "Передача в 1С:ДО",
            "action_text": "Заявка будет передана во внешний документооборот и получит внешний идентификатор.",
            "submit_label": "Передать в 1С:ДО",
            "permission_error": "Передавать заявки может экономист или администратор",
            "can_execute": lambda user, document: _can_manage_payment_requests(user),
            "execute": lambda document, user, comment: transfer_payment_request_to_do(document, user),
            "success_message": "Заявка {number} передана в 1С:ДО",
            "details": _payment_request_action_details,
        },
    }
    config = configs.get((document_type, action))
    if config is None:
        return None
    resolved = dict(config)
    resolved["document"] = config["document"]()
    return resolved


def _budget_action_details(budget):
    return [
        {"label": "Номер", "value": budget.number},
        {"label": "Компания", "value": budget.organization.name if budget.organization else "Не указана"},
        {"label": "Год", "value": budget.budget_year},
        {"label": "Вид", "value": budget.get_scope_display()},
        {"label": "Сумма", "value": f"{budget.total_amount} {budget.currency.code}"},
        {"label": "Статус", "value": budget.get_status_display()},
        {"label": "Автор", "value": budget.author.get_full_name() or budget.author.username},
    ]


def _limit_action_details(plan):
    return [
        {"label": "Номер", "value": plan.number},
        {"label": "Компания", "value": plan.organization.name if plan.organization else "Не указана"},
        {"label": "Бюджет", "value": plan.budget.number if plan.budget else "Без бюджета"},
        {"label": "ЦФО", "value": plan.department.name},
        {"label": "Статья", "value": plan.article.name},
        {"label": "Сумма", "value": f"{plan.annual_amount} {plan.currency.code}"},
        {"label": "Согласующий", "value": plan.approver.get_full_name() or plan.approver.username},
    ]


def _limit_adjustment_action_details(adjustment):
    return [
        {"label": "Номер", "value": adjustment.number},
        {"label": "Базовый лимит", "value": adjustment.base_plan.number},
        {"label": "Компания-источник", "value": adjustment.base_plan.organization.name if adjustment.base_plan.organization else "Не указана"},
        {"label": "Компания-получатель", "value": adjustment.target_organization.name if adjustment.target_organization else "Внутри компании"},
        {"label": "ЦФО", "value": adjustment.base_plan.department.name},
        {"label": "Статья", "value": adjustment.article.name},
        {"label": "Новая сумма", "value": f"{adjustment.new_annual_amount} {adjustment.base_plan.currency.code}"},
        {"label": "Версия", "value": f"v{adjustment.version}"},
    ]


def _payment_request_action_details(payment_request):
    return [
        {"label": "Номер", "value": payment_request.number},
        {"label": "Компания", "value": payment_request.organization.name},
        {"label": "Тип", "value": payment_request.get_request_kind_display()},
        {"label": "Контрагент", "value": payment_request.counterparty.name},
        {"label": "Сумма", "value": f"{payment_request.amount} {payment_request.currency.code}"},
        {"label": "Статус", "value": payment_request.get_status_display()},
        {"label": "Согласующий", "value": payment_request.approver.get_full_name() or payment_request.approver.username},
    ]


def _working_organization(request):
    organization_id = request.session.get("working_organization_id")
    organization = None
    if organization_id:
        organization = Organization.objects.filter(pk=organization_id).first()
    if organization is None:
        organization = Organization.objects.order_by("name").first()
    if organization is None:
        return None
    request.session["working_organization_id"] = organization.id
    return organization


def _build_workday_context(user, organization) -> dict:
    profile = getattr(user, "profile", None)
    role = profile.role if profile else ""
    tasks = []

    if organization is None:
        tasks.append(
            _task_row(
                "Подготовить компании",
                "Перед работой с бюджетами и лимитами синхронизируйте или заведите компанию.",
                "НСИ",
                reverse("nsi_dashboard") if user.is_superuser or role in [UserRole.ADMINISTRATOR, UserRole.ECONOMIST] else reverse("workspace"),
                "Открыть",
                "high",
            )
        )
        return {
            "role_label": _workday_role_label(role),
            "headline": _workday_headline(role),
            "rhythm": _workday_rhythm(role),
            "tasks": tasks,
            "task_count": len(tasks),
            "high_count": 1,
            "primary_action": _workday_primary_action(role),
        }

    if _can_approve_budgets(user):
        for budget in BudgetPlan.objects.filter(
            organization=organization,
            status=BudgetPlanStatus.PENDING_APPROVAL,
        ).select_related("currency")[:5]:
            tasks.append(
                _task_row(
                    "Согласовать бюджет",
                    f"{budget.number} · {budget.budget_year} · {budget.total_amount} {budget.currency.code}",
                    "Планирование",
                    reverse("document_action_wizard", args=["budget", budget.id, "approve"]),
                    "Открыть wizard",
                    "high",
                )
            )

    if _can_approve_plans(user):
        plan_query = BudgetLimitPlan.objects.filter(
            organization=organization,
            status=BudgetPlanStatus.PENDING_APPROVAL,
        ).select_related(
            "department",
            "article",
            "currency",
            "approver",
        )
        adjustment_query = BudgetLimitAdjustment.objects.filter(
            base_plan__organization=organization,
            status=BudgetPlanStatus.PENDING_APPROVAL,
        ).select_related(
            "base_plan",
            "base_plan__currency",
            "article",
            "approver",
        )
        if not user.is_superuser:
            plan_query = plan_query.filter(approver=user)
            adjustment_query = adjustment_query.filter(approver=user)
        for plan in plan_query[:5]:
            tasks.append(
                _task_row(
                    "Утвердить лимит",
                    f"{plan.number} · {plan.department.name} · {plan.annual_amount} {plan.currency.code}",
                    "Лимиты",
                    reverse("document_action_wizard", args=["limit", plan.id, "approve"]),
                    "Открыть wizard",
                    "high",
                )
            )
        for adjustment in adjustment_query[:5]:
            tasks.append(
                _task_row(
                    "Утвердить корректировку",
                    f"{adjustment.number} · {adjustment.base_plan.number} · v{adjustment.version}",
                    "Лимиты",
                    reverse("document_action_wizard", args=["limit-adjustment", adjustment.id, "approve"]),
                    "Открыть wizard",
                    "high",
                )
            )

    if _can_approve_payment_requests(user):
        payment_query = PaymentRequest.objects.filter(
            organization=organization,
            status=PaymentRequestStatus.PENDING_APPROVAL,
        ).select_related(
            "counterparty",
            "currency",
            "approver",
        )
        if not user.is_superuser:
            payment_query = payment_query.filter(approver=user)
        for payment_request in payment_query[:7]:
            tasks.append(
                _task_row(
                    "Согласовать платеж",
                    f"{payment_request.number} · {payment_request.counterparty.name} · {payment_request.amount} {payment_request.currency.code}",
                    "Платежи",
                    reverse("document_action_wizard", args=["payment-request", payment_request.id, "approve"]),
                    "Открыть wizard",
                    "high",
                )
            )

    if _can_manage_payment_requests(user):
        editable_requests = PaymentRequest.objects.filter(
            Q(status=PaymentRequestStatus.REJECTED) | Q(status=PaymentRequestStatus.DRAFT),
            organization=organization,
        ).select_related("counterparty", "currency", "author")
        if not user.is_superuser:
            editable_requests = editable_requests.filter(author=user)
        for payment_request in editable_requests[:5]:
            action_url = (
                reverse("payment_request_edit_wizard", args=[payment_request.id])
                if payment_request.status == PaymentRequestStatus.REJECTED
                else reverse("document_action_wizard", args=["payment-request", payment_request.id, "submit"])
            )
            tasks.append(
                _task_row(
                    "Доработать заявку" if payment_request.status == PaymentRequestStatus.REJECTED else "Отправить черновик",
                    f"{payment_request.number} · {payment_request.counterparty.name} · {payment_request.get_status_display()}",
                    "Платежи",
                    action_url,
                    "Открыть wizard",
                    "medium",
                )
            )

        for payment_request in PaymentRequest.objects.filter(
            organization=organization,
            status=PaymentRequestStatus.APPROVED,
        ).select_related(
            "counterparty",
            "currency",
        )[:7]:
            tasks.append(
                _task_row(
                    "Передать в 1С:ДО",
                    f"{payment_request.number} · {payment_request.counterparty.name} · {payment_request.amount} {payment_request.currency.code}",
                    "Платежи",
                    reverse("document_action_wizard", args=["payment-request", payment_request.id, "transfer"]),
                    "Открыть wizard",
                    "medium",
                )
            )

    if _can_edit_plans(user):
        draft_limits = BudgetLimitPlan.objects.filter(
            organization=organization,
            status=BudgetPlanStatus.DRAFT,
        ).select_related(
            "department",
            "article",
            "currency",
            "author",
        )
        draft_adjustments = BudgetLimitAdjustment.objects.filter(
            base_plan__organization=organization,
            status=BudgetPlanStatus.DRAFT,
        ).select_related(
            "base_plan",
            "base_plan__currency",
            "author",
        )
        if not user.is_superuser:
            draft_limits = draft_limits.filter(author=user)
            draft_adjustments = draft_adjustments.filter(author=user)
        for plan in draft_limits[:4]:
            tasks.append(
                _task_row(
                    "Отправить лимит",
                    f"{plan.number} · {plan.department.name} · {plan.annual_amount} {plan.currency.code}",
                    "Планирование",
                    reverse("document_action_wizard", args=["limit", plan.id, "submit"]),
                    "Открыть wizard",
                    "medium",
                )
            )
        for adjustment in draft_adjustments[:4]:
            tasks.append(
                _task_row(
                    "Отправить корректировку",
                    f"{adjustment.number} · {adjustment.base_plan.number} · v{adjustment.version}",
                    "Планирование",
                    reverse("document_action_wizard", args=["limit-adjustment", adjustment.id, "submit"]),
                    "Открыть wizard",
                    "medium",
                )
            )

    if _is_administrator(user):
        for item in IntegrationRequest.objects.filter(status__in=[
            IntegrationRequestStatus.NEW,
            IntegrationRequestStatus.IN_PROGRESS,
        ]).select_related("requested_by")[:6]:
            tasks.append(
                _task_row(
                    "Вести интеграцию",
                    f"{item.number} · {item.integration_name} · {item.target_system}",
                    "Настройки",
                    reverse("integration_request_status_wizard", args=[item.id]),
                    "Открыть wizard",
                    "medium",
                )
            )

    if not tasks:
        tasks.append(
            _task_row(
                "Создать рабочий документ",
                "Начните с первичного бюджета, корректировки лимита или платежной заявки.",
                "Старт",
                reverse("planning_wizard") if _can_create_budgets(user) else reverse("payment_requests"),
                "Начать",
                "low",
            )
        )

    tasks = tasks[:12]
    return {
        "role_label": _workday_role_label(role),
        "headline": _workday_headline(role),
        "rhythm": _workday_rhythm(role),
        "tasks": tasks,
        "task_count": len(tasks),
        "high_count": sum(1 for task in tasks if task["priority"] == "high"),
        "primary_action": _workday_primary_action(role),
    }


def _task_row(title: str, text: str, area: str, url: str, action_label: str, priority: str) -> dict:
    return {
        "title": title,
        "text": text,
        "area": area,
        "url": url,
        "action_label": action_label,
        "priority": priority,
    }


def _workday_role_label(role: str) -> str:
    labels = {
        UserRole.ADMINISTRATOR: "Администратор",
        UserRole.ECONOMIST: "Экономист",
        UserRole.MANAGER: "Руководитель",
        UserRole.ACCOUNTANT: "Бухгалтер",
    }
    return labels.get(role, "Пользователь")


def _workday_headline(role: str) -> str:
    if role == UserRole.MANAGER:
        return "Утром проверьте документы на утверждении, затем отклонения и превышения."
    if role == UserRole.ADMINISTRATOR:
        return "Начните с зависших согласований и интеграционных заявок, затем проверьте журналы."
    if role == UserRole.ECONOMIST:
        return "Сначала доработайте возвраты и отправьте черновики, затем создавайте новые документы."
    return "Начните с документов, которые требуют действия сегодня."


def _workday_rhythm(role: str) -> list[dict]:
    if role == UserRole.MANAGER:
        return [
            {"time": "09:00", "title": "Очередь согласований", "text": "Бюджеты, лимиты, корректировки и платежные заявки."},
            {"time": "12:00", "title": "Контроль лимитов", "text": "Проверка превышений, резервов и спорных заявок."},
            {"time": "16:00", "title": "Отчеты", "text": "План-факт и управленческий дашборд."},
        ]
    if role == UserRole.ADMINISTRATOR:
        return [
            {"time": "09:00", "title": "Зависшие операции", "text": "Документы без движения и интеграционные запросы."},
            {"time": "13:00", "title": "НСИ и доступы", "text": "Синхронизация, роли, справочники."},
            {"time": "17:00", "title": "Аудит", "text": "Проверка журнала действий и качества данных."},
        ]
    return [
        {"time": "09:00", "title": "Возвраты и черновики", "text": "Исправить отклоненные заявки и отправить готовые документы."},
        {"time": "11:00", "title": "Новый ввод", "text": "Бюджеты, лимиты, платежные заявки и корректировки через wizard."},
        {"time": "15:00", "title": "Контроль журналов", "text": "Статусы, передача в 1С:ДО, факты и план-факт."},
    ]


def _workday_primary_action(role: str) -> dict:
    if role == UserRole.MANAGER:
        return {"label": "Открыть дашборд", "url": reverse("manager_dashboard")}
    if role == UserRole.ADMINISTRATOR:
        return {"label": "Интеграции", "url": reverse("integration_requests")}
    return {"label": "Новая заявка", "url": reverse("payment_request_wizard")}


def _can_edit_plans(user) -> bool:
    return _has_model_permission(user, BudgetLimitPlan, "change")


def _can_create_budgets(user) -> bool:
    return _has_model_permission(user, BudgetPlan, "add")


def _can_approve_budgets(user) -> bool:
    return _can_approve_plans(user) and _has_model_permission(user, BudgetPlan, "view")


def _can_create_plans(user) -> bool:
    return _has_model_permission(user, BudgetLimitPlan, "add")


def _can_create_limit_adjustments(user) -> bool:
    return _has_model_permission(user, BudgetLimitAdjustment, "add")


def _can_edit_limit_adjustments(user) -> bool:
    return _has_model_permission(user, BudgetLimitAdjustment, "change")


def _can_delete_plans(user) -> bool:
    return _has_model_permission(user, BudgetLimitPlan, "delete")


def _can_approve_plans(user) -> bool:
    if user.is_superuser:
        return True
    profile = getattr(user, "profile", None)
    return bool(profile and profile.role in [UserRole.ADMINISTRATOR, UserRole.MANAGER])


def _can_approve_payment_requests(user) -> bool:
    return _can_approve_plans(user) and _has_model_permission(user, PaymentRequest, "view")


def _can_manage_payment_requests(user) -> bool:
    return _has_model_permission(user, PaymentRequest, "change")


def _can_create_payment_requests(user) -> bool:
    return _has_model_permission(user, PaymentRequest, "add")


def _can_delete_payment_requests(user) -> bool:
    return _has_model_permission(user, PaymentRequest, "delete")


def _can_edit_payment_facts(user) -> bool:
    return _has_model_permission(user, PaymentFact, "change")


def _can_view_payment_facts(user) -> bool:
    return _has_model_permission(user, PaymentFact, "view")


def _can_create_payment_facts(user) -> bool:
    return _has_model_permission(user, PaymentFact, "add")


def _can_delete_payment_facts(user) -> bool:
    return _has_model_permission(user, PaymentFact, "delete")


def _can_manage_report_templates(user) -> bool:
    return _has_model_permission(user, ReportTemplate, "add")


def _is_administrator(user) -> bool:
    if user.is_superuser:
        return True
    profile = getattr(user, "profile", None)
    return bool(profile and profile.role == UserRole.ADMINISTRATOR)


def _has_model_permission(user, model, action: str) -> bool:
    if not user.is_authenticated or not user.is_active:
        return False
    if user.is_superuser:
        return True
    return user.has_perm(f"{model._meta.app_label}.{action}_{model._meta.model_name}")


_IMAGE_PATTERN = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_ORDERED_LIST_PATTERN = re.compile(r"\d+\.\s+(.*)")
_UNORDERED_LIST_PATTERN = re.compile(r"-\s+(.*)")


def _render_user_guide_markdown(markdown_text: str) -> str:
    lines = markdown_text.splitlines()
    chunks = ['<article class="guide-content">']
    index = 0

    while index < len(lines):
        line = lines[index].rstrip()
        stripped = line.strip()

        if not stripped:
            index += 1
            continue

        if stripped.startswith("### "):
            chunks.append(f"<h3>{_render_inline_markdown(stripped[4:])}</h3>")
            index += 1
            continue
        if stripped.startswith("## "):
            chunks.append(f"<h2>{_render_inline_markdown(stripped[3:])}</h2>")
            index += 1
            continue
        if stripped.startswith("# "):
            chunks.append(f"<h1>{_render_inline_markdown(stripped[2:])}</h1>")
            index += 1
            continue
        if stripped == "---":
            chunks.append("<hr>")
            index += 1
            continue

        image_match = _IMAGE_PATTERN.fullmatch(stripped)
        if image_match:
            alt_text = _render_inline_markdown(image_match.group(1))
            image_src = image_match.group(2).strip()
            image_url = reverse("instruction_asset", kwargs={"asset_path": image_src})
            chunks.append(
                (
                    '<figure class="guide-figure">'
                    f'<img src="{image_url}" alt="{html.escape(image_match.group(1))}" loading="lazy">'
                    f"<figcaption>{alt_text}</figcaption>"
                    "</figure>"
                )
            )
            index += 1
            continue

        if _is_table_line(stripped):
            table_lines = []
            while index < len(lines) and _is_table_line(lines[index].strip()):
                table_lines.append(lines[index].strip())
                index += 1
            chunks.append(_render_markdown_table(table_lines))
            continue

        ordered_match = _ORDERED_LIST_PATTERN.match(stripped)
        if ordered_match:
            list_items = []
            while index < len(lines):
                current_line = lines[index].strip()
                match = _ORDERED_LIST_PATTERN.match(current_line)
                if not match:
                    break
                list_items.append(f"<li>{_render_inline_markdown(match.group(1))}</li>")
                index += 1
            chunks.append("<ol>" + "".join(list_items) + "</ol>")
            continue

        unordered_match = _UNORDERED_LIST_PATTERN.match(stripped)
        if unordered_match:
            list_items = []
            while index < len(lines):
                current_line = lines[index].strip()
                match = _UNORDERED_LIST_PATTERN.match(current_line)
                if not match:
                    break
                list_items.append(f"<li>{_render_inline_markdown(match.group(1))}</li>")
                index += 1
            chunks.append("<ul>" + "".join(list_items) + "</ul>")
            continue

        paragraph_lines = [stripped]
        index += 1
        while index < len(lines):
            current_line = lines[index].strip()
            if not current_line:
                break
            if _starts_block(current_line):
                break
            paragraph_lines.append(current_line)
            index += 1
        paragraph = " ".join(paragraph_lines)
        chunks.append(f"<p>{_render_inline_markdown(paragraph)}</p>")

    chunks.append("</article>")
    return "".join(chunks)


def _render_markdown_table(table_lines: list[str]) -> str:
    if not table_lines:
        return ""

    rows = [[cell.strip() for cell in line.strip("|").split("|")] for line in table_lines]
    if not rows:
        return ""

    separator_index = None
    if len(rows) > 1 and all(set(cell.replace(":", "").replace("-", "").strip()) == set() for cell in rows[1]):
        separator_index = 1

    headers = rows[0]
    data_rows = rows[2:] if separator_index is not None else rows[1:]

    head_html = "".join(f"<th>{_render_inline_markdown(header)}</th>" for header in headers)
    body_html = "".join(
        "<tr>" + "".join(f"<td>{_render_inline_markdown(cell)}</td>" for cell in row) + "</tr>"
        for row in data_rows
    )
    return (
        '<div class="data-table-wrap guide-table-wrap"><table class="data-table guide-table">'
        f"<thead><tr>{head_html}</tr></thead><tbody>{body_html}</tbody></table></div>"
    )


def _render_inline_markdown(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    return escaped


def _is_table_line(line: str) -> bool:
    return line.startswith("|") and line.endswith("|")


def _starts_block(line: str) -> bool:
    return (
        line.startswith("#")
        or line == "---"
        or bool(_IMAGE_PATTERN.fullmatch(line))
        or bool(_ORDERED_LIST_PATTERN.match(line))
        or bool(_UNORDERED_LIST_PATTERN.match(line))
        or _is_table_line(line)
    )
