"""Перечисления (TextChoices), общий валидатор цвета и абстрактная база моделей.

Базовый слой: ни от чего внутри пакета не зависит. Все доменные модули
импортируют отсюда choices и `IntegrationTrackedModel`.
"""

from django.core.validators import RegexValidator
from django.db import models


hex_color_validator = RegexValidator(
    regex=r"^#[0-9A-Fa-f]{6}$",
    message="Цвет должен быть в формате #RRGGBB",
)


class SourceSystem(models.TextChoices):
    MANUAL = "manual", "Создано вручную"
    ONE_C_BP = "1c_bp", "1С:Бухгалтерия предприятия"
    ONE_C_DO = "1c_do", "1С:Документооборот"
    MOCK_ONE_C = "mock_1c", "Mock-1C"


class ContractKind(models.TextChoices):
    CUSTOMER = "customer", "Договор с покупателем"
    SOLE_SUPPLIER = "sole_supplier", "Договор с единственным поставщиком"
    LOAN_RECEIVED = "loan_received", "Договор займа (получен нами)"
    LOAN_GIVEN = "loan_given", "Договор займа (выдан нами)"


class FundingCaseStatus(models.TextChoices):
    DRAFT = "draft", "Черновик"
    ACTIVE = "active", "В работе"
    CLOSED = "closed", "Закрыт"
    CANCELLED = "cancelled", "Отменён"


class PaymentDirection(models.TextChoices):
    INFLOW = "inflow", "Поступление денежных средств"
    OUTFLOW = "outflow", "Списание денежных средств"


class AccountingKind(models.TextChoices):
    BU = "bu", "БУ"
    NU = "nu", "НУ"


class SyncStatus(models.TextChoices):
    SUCCESS = "success", "Успешно"
    FAILED = "failed", "Ошибка"


class UserRole(models.TextChoices):
    ADMINISTRATOR = "administrator", "Администратор"
    ECONOMIST = "economist", "Экономист"
    MANAGER = "manager", "Руководитель"
    ACCOUNTANT = "accountant", "Бухгалтер"


class UiThemeMode(models.TextChoices):
    LIGHT = "light", "Светлая"
    DARK = "dark", "Темная"


class IntegrationRequestStatus(models.TextChoices):
    NEW = "new", "Новая"
    IN_PROGRESS = "in_progress", "В работе"
    COMPLETED = "completed", "Выполнена"
    REJECTED = "rejected", "Отклонена"


class NotificationKind(models.TextChoices):
    # Заявки на оплату
    PAYMENT_SUBMITTED = "payment_submitted", "Заявка ушла на согласование"
    PAYMENT_APPROVED = "payment_approved", "Заявка согласована"
    PAYMENT_REJECTED = "payment_rejected", "Заявка отклонена"
    PAYMENT_TRANSFERRED = "payment_transferred", "Заявка передана в 1С:ДО"
    # Лимиты и бюджеты
    BUDGET_SUBMITTED = "budget_submitted", "Бюджет на утверждении"
    LIMIT_SUBMITTED = "limit_submitted", "Лимит на утверждении"
    LIMIT_APPROVED = "limit_approved", "Лимит утверждён"
    LIMIT_ADJUSTMENT_SUBMITTED = "limit_adjustment_submitted", "Корректировка лимита на утверждении"
    LIMIT_OVERRUN = "limit_overrun", "Превышение лимита"
    # Внешний контур
    EXTERNAL_PAYMENT_POSTED = "external_payment_posted", "Внешняя оплата проведена"
    # Интеграции
    INTEGRATION_STATUS_CHANGED = "integration_status_changed", "Статус интеграционной заявки изменён"


class AuditAction(models.TextChoices):
    VIEW = "view", "Просмотр"
    CREATE = "create", "Создание"
    UPDATE = "update", "Редактирование"
    DELETE = "delete", "Удаление"
    APPROVE = "approve", "Утверждение"
    PAY = "pay", "Оплата"
    TRANSFER = "transfer", "Передача"
    LOGIN = "login", "Вход"
    DENIED = "denied", "Отказ доступа"


class ExternalPaymentStatus(models.TextChoices):
    DRAFT = "draft", "Черновик"
    POSTED = "posted", "Проведен"


class BudgetPlanStatus(models.TextChoices):
    DRAFT = "draft", "Черновик"
    PENDING_APPROVAL = "pending_approval", "На утверждении"
    APPROVED = "approved", "Утвержден"
    CLOSED = "closed", "Закрыт"


class CashFlowDirection(models.TextChoices):
    OUTFLOW = "outflow", "Выплаты"
    INFLOW = "inflow", "Поступления"
    INTERNAL = "internal", "Внутренние обороты"
    TRANSFER = "transfer", "Переводы между счетами"


class PlanningScenarioKind(models.TextChoices):
    BASE = "base", "Базовый"
    OPTIMISTIC = "optimistic", "Оптимистичный"
    PESSIMISTIC = "pessimistic", "Пессимистичный"
    CUSTOM = "custom", "Пользовательский"


class BudgetPeriodicity(models.TextChoices):
    YEAR = "year", "Год"


class BudgetScope(models.TextChoices):
    OVERALL = "overall", "Общий"
    BY_DEPARTMENT = "by_department", "По ЦФО"


class PaymentRequestKind(models.TextChoices):
    BY_CONTRACT = "by_contract", "По договору"
    BY_INVOICE = "by_invoice", "По счету"
    WITHOUT_CONTRACT = "without_contract", "Без договора"


class PaymentRequestStatus(models.TextChoices):
    DRAFT = "draft", "Черновик"
    PENDING_APPROVAL = "pending_approval", "На согласовании"
    PENDING_FINAL_APPROVAL = "pending_final_approval", "На финальном согласовании"
    APPROVED = "approved", "Согласована"
    TRANSFERRED = "transferred", "Передана в 1С:ДО"
    REJECTED = "rejected", "Отклонена"
    CANCELLED = "cancelled", "Отменена автором"


class PaymentLimitControlMode(models.TextChoices):
    WARNING = "warning", "Предупреждение"
    BLOCK = "block", "Блокировка"


class ReportTemplateType(models.TextChoices):
    PLAN_FACT = "plan_fact", "План-факт БДДС"


class IntegrationTrackedModel(models.Model):
    external_id = models.CharField("ID во внешней системе", max_length=128, blank=True)
    source_system = models.CharField(
        "Источник",
        max_length=32,
        choices=SourceSystem.choices,
        default=SourceSystem.MANUAL,
    )
    synced_at = models.DateTimeField("Дата синхронизации", null=True, blank=True)

    class Meta:
        abstract = True
