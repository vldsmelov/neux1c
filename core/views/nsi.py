"""НСИ — справочники: dashboard + CRUD по конкретному directory.

Самодостаточный модуль: содержит `NSI_DIRECTORY_CONFIG` (декларативное описание
шести справочников) + два view и набор приватных helpers для CRUD/валидации.
"""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models.deletion import ProtectedError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from ..access import role_required
from ..integrations.one_c.mock import MockOneCProvider
from ..models import (
    AuditAction,
    AuditLog,
    CashFlowArticle,
    Counterparty,
    Currency,
    Department,
    Nomenclature,
    Organization,
    SourceSystem,
    SyncRun,
    UserRole,
)
from ..services.one_c_sync import sync_one_c_dataset
from ._shared import can_sync_mock_1c, has_model_permission


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


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def nsi_dashboard(request):
    default_directory = next(iter(NSI_DIRECTORY_CONFIG))
    default_path = reverse(
        "nsi_directory",
        kwargs={"directory": request.POST.get("directory") or default_directory},
    )

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "sync":
                if not can_sync_mock_1c(request.user):
                    raise ValueError("Недостаточно прав для синхронизации Mock-1С")
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


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def nsi_directory(request, directory: str):
    config = _nsi_directory_config(directory)
    model = config["model"]
    path = reverse("nsi_directory", kwargs={"directory": directory})

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "sync":
                if not can_sync_mock_1c(request.user):
                    raise ValueError("Недостаточно прав для синхронизации Mock-1С")
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
    return render(
        request,
        "core/nsi_directory.html",
        {
            "latest_sync": SyncRun.objects.first(),
            "managed_directories": _nsi_directory_overview(),
            "directory": config,
            "directory_key": directory,
            "directory_rows": [_nsi_directory_object_row(obj, config) for obj in objects],
            "can_create_nsi": has_model_permission(request.user, model, "add"),
            "can_update_nsi": has_model_permission(request.user, model, "change"),
            "can_delete_nsi": has_model_permission(request.user, model, "delete"),
            "can_sync_mock_1c": can_sync_mock_1c(request.user),
            "active_section": "nsi",
        },
    )


# --- Helpers (private to this module) -------------------------------------

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
    if not has_model_permission(request.user, model, "add"):
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
    if not has_model_permission(request.user, model, "change"):
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
    if not has_model_permission(request.user, model, "delete"):
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
        raw_value = (
            _required_post_value(request, field_name, label)
            if field.get("required")
            else (request.POST.get(field_name) or "").strip()
        )
        if field.get("transform") == "upper":
            raw_value = raw_value.upper()
        values[field_name] = raw_value
    return values


def _nsi_directory_object_row(obj, config: dict) -> dict:
    return {
        "id": obj.pk,
        "object": obj,
        "source": obj.get_source_system_display(),
        "source_code": obj.source_system,
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
        return "; ".join(f"{field}: {', '.join(msgs)}" for field, msgs in exc.message_dict.items())
    if isinstance(exc, ProtectedError):
        return "Запись уже используется в документах, поэтому ее нельзя удалить"
    if isinstance(exc, IntegrityError):
        return "Такой элемент НСИ уже существует или нарушает уникальность справочника"
    return str(exc)


def can_create_nsi(user) -> bool:
    """Public helper kept for any future callers; unused right now."""
    return any(has_model_permission(user, model, "add") for model in NSI_DIRECTORY_MODELS.values())
