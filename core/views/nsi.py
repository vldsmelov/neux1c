"""НСИ — справочники: dashboard + CRUD по конкретному directory.

Самодостаточный модуль: содержит `NSI_DIRECTORY_CONFIG` (декларативное описание
шести справочников) + два view и набор приватных helpers для CRUD/валидации.
"""

import csv
from io import StringIO, TextIOWrapper

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

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
        "description": "Классификатор движения денежных средств (БДДС): иерархия + направление потока",
        "ordering": ["code"],
        "fields": [
            {"name": "code", "label": "Код", "required": True, "transform": "upper", "placeholder": "DDS-040"},
            {"name": "name", "label": "Наименование", "required": True, "placeholder": "Командировочные расходы"},
            {
                "name": "direction",
                "label": "Направление потока",
                "kind": "choice",
                "choices": [
                    ("outflow", "Выплаты"),
                    ("inflow", "Поступления"),
                    ("internal", "Внутренние обороты"),
                    ("transfer", "Переводы"),
                ],
                "default": "outflow",
                "required": True,
            },
            {
                "name": "parent",
                "label": "Родительская группа",
                "kind": "fk_self",
                "options_from": "self",
            },
            {"name": "is_group", "label": "Группа", "kind": "checkbox"},
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

    if request.method == "GET" and request.GET.get("export") == "csv":
        return _export_nsi_csv(directory, config)

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
            elif action == "import_csv":
                stats = _import_nsi_csv(request, directory, config, path=path)
                messages.success(
                    request,
                    f"Импорт CSV: создано {stats['created']}, пропущено {stats['skipped']}, ошибок {stats['errors']}",
                )
            else:
                raise ValueError("Неизвестное действие НСИ")
        except (IntegrityError, ProtectedError, ValidationError, ValueError) as exc:
            messages.error(request, _format_nsi_error(exc))
        return redirect(path)

    objects = model.objects.order_by(*config["ordering"])
    # Populate fk_self options at view time so the create form has them too
    enriched_fields = []
    for field in config["fields"]:
        enriched = dict(field)
        if field.get("kind") == "fk_self" and field.get("options_from") == "self":
            enriched["options"] = [
                {"id": item.pk, "label": str(item)} for item in model.objects.order_by(*config["ordering"])
            ]
        enriched_fields.append(enriched)
    directory_context = {**config, "fields": enriched_fields}
    return render(
        request,
        "core/nsi_directory.html",
        {
            "latest_sync": SyncRun.objects.first(),
            "managed_directories": _nsi_directory_overview(),
            "directory": directory_context,
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
        if field.get("kind") == "choice":
            raw_value = (request.POST.get(field_name) or "").strip()
            allowed = {value for value, _ in field.get("choices", [])}
            if raw_value not in allowed:
                if field.get("required"):
                    raise ValueError(f"Заполните поле «{field['label']}»")
                raw_value = field.get("default", "")
            values[field_name] = raw_value
            continue
        if field.get("kind") == "fk_self":
            raw_value = (request.POST.get(field_name) or "").strip()
            values[field_name + "_id"] = int(raw_value) if raw_value.isdigit() else None
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
    if field.get("kind") == "fk_self":
        related = getattr(obj, field["name"])
        value = related
        input_value = related.pk if related else ""
        display_value = str(related) if related else "—"
        field_data = {
            **field,
            "value": value,
            "input_value": input_value,
            "display_value": display_value,
            "options": [
                {"id": item.pk, "label": str(item)}
                for item in type(obj).objects.exclude(pk=obj.pk).order_by("code")
            ] if field.get("options_from") == "self" else [],
        }
        return field_data
    if field.get("kind") == "choice":
        value = getattr(obj, field["name"])
        display_method = getattr(obj, f"get_{field['name']}_display", None)
        display_value = display_method() if callable(display_method) else value
        field_data = {
            **field,
            "value": value,
            "input_value": value or "",
            "display_value": display_value,
        }
        return field_data
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


def _export_nsi_csv(directory: str, config: dict) -> HttpResponse:
    model = config["model"]
    field_names = [field["name"] for field in config["fields"]]
    headers = [field["label"] for field in config["fields"]] + ["Источник"]
    buffer = StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(headers)
    for obj in model.objects.order_by(*config["ordering"]).iterator():
        row = []
        for name in field_names:
            value = getattr(obj, name)
            if isinstance(value, bool):
                row.append("1" if value else "0")
            else:
                row.append("" if value is None else str(value))
        row.append(obj.get_source_system_display())
        writer.writerow(row)
    response = HttpResponse("﻿" + buffer.getvalue(), content_type="text/csv; charset=utf-8")
    stamp = timezone.localdate().strftime("%Y%m%d")
    response["Content-Disposition"] = f'attachment; filename="nsi_{directory}_{stamp}.csv"'
    return response


def _import_nsi_csv(request, directory: str, config: dict, *, path: str) -> dict:
    model = config["model"]
    if not has_model_permission(request.user, model, "add"):
        raise ValueError("Недостаточно прав для импорта элементов НСИ")
    upload = request.FILES.get("csv_file")
    if upload is None:
        raise ValueError("Не выбран CSV-файл для импорта")

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
        raise ValueError("CSV-файл пустой или поврежден")

    label_to_field = {field["label"].strip().lower(): field for field in config["fields"]}
    name_to_field = {field["name"]: field for field in config["fields"]}
    column_map: dict[str, dict] = {}
    for column in reader.fieldnames:
        key = (column or "").strip().lower()
        if key in label_to_field:
            column_map[column] = label_to_field[key]
        elif key in name_to_field:
            column_map[column] = name_to_field[key]

    required_missing = [f["label"] for f in config["fields"] if f.get("required") and f not in column_map.values()]
    if required_missing:
        raise ValueError(f"В CSV отсутствуют обязательные колонки: {', '.join(required_missing)}")

    created = skipped = errors = 0
    with transaction.atomic():
        for row_index, row in enumerate(reader, start=2):
            values: dict = {}
            try:
                for column, field in column_map.items():
                    raw = (row.get(column) or "").strip()
                    if field.get("kind") == "checkbox":
                        values[field["name"]] = raw.lower() in {"1", "true", "yes", "да", "x"}
                        continue
                    if field.get("transform") == "upper":
                        raw = raw.upper()
                    if field.get("required") and not raw:
                        raise ValueError(f"строка {row_index}: пустое значение в колонке «{field['label']}»")
                    values[field["name"]] = raw
                # Defaults for checkbox fields not present in CSV
                for field in config["fields"]:
                    if field["name"] not in values and field.get("kind") == "checkbox":
                        values[field["name"]] = bool(field.get("default"))
                values.update(config.get("create_defaults", {}))
                item = model(**values, source_system=SourceSystem.MANUAL)
                item.full_clean()
                item.save()
                created += 1
            except (ValidationError, ValueError) as exc:
                # Duplicates -> skipped; other validation problems -> errors
                if isinstance(exc, ValidationError) and any(
                    "уже сущест" in m.lower() or "unique" in m.lower()
                    for m in getattr(exc, "messages", [])
                ):
                    skipped += 1
                else:
                    errors += 1
            except IntegrityError:
                skipped += 1

    AuditLog.objects.create(
        user=request.user,
        action=AuditAction.CREATE,
        path=path,
        object_type=model.__name__,
        object_id="",
        message=f"CSV-импорт НСИ {directory}: создано {created}, пропущено {skipped}, ошибок {errors}",
    )
    return {"created": created, "skipped": skipped, "errors": errors}


def can_create_nsi(user) -> bool:
    """Public helper kept for any future callers; unused right now."""
    return any(has_model_permission(user, model, "add") for model in NSI_DIRECTORY_MODELS.values())
