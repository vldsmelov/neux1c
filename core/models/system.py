"""Системные сущности: профиль пользователя, тема, аудит, уведомления,
нумерация документов, журнал синхронизаций, шаблоны отчётов, заявки на интеграцию."""

from django.conf import settings
from django.db import models
from django.utils import timezone

from .enums import (
    AuditAction,
    IntegrationRequestStatus,
    NotificationKind,
    ReportTemplateType,
    SyncStatus,
    UiThemeMode,
    UserRole,
    hex_color_validator,
)
from .nsi import Department


class ReportTemplate(models.Model):
    name = models.CharField("Название", max_length=120)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Владелец",
        on_delete=models.CASCADE,
        related_name="report_templates",
    )
    report_type = models.CharField("Тип отчета", max_length=32, choices=ReportTemplateType.choices)
    filters = models.JSONField("Настройки фильтров", default=dict, blank=True)
    is_default = models.BooleanField("По умолчанию", default=False)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Шаблон отчета"
        verbose_name_plural = "Шаблоны отчетов"
        constraints = [
            models.UniqueConstraint(fields=["owner", "report_type", "name"], name="uniq_report_template_owner_type_name"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_report_type_display()})"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_default:
            ReportTemplate.objects.filter(owner=self.owner, report_type=self.report_type).exclude(pk=self.pk).update(
                is_default=False
            )


class SyncRun(models.Model):
    provider = models.CharField("Поставщик", max_length=64, default="mock_1c")
    status = models.CharField("Статус", max_length=16, choices=SyncStatus.choices)
    started_at = models.DateTimeField("Начало", default=timezone.now)
    finished_at = models.DateTimeField("Окончание", null=True, blank=True)
    message = models.TextField("Сообщение", blank=True)
    stats = models.JSONField("Статистика", default=dict, blank=True)

    class Meta:
        ordering = ["-started_at"]
        verbose_name = "Запуск синхронизации"
        verbose_name_plural = "Запуски синхронизации"

    def __str__(self) -> str:
        return f"{self.provider} {self.started_at:%d.%m.%Y %H:%M}"


class DocumentSequence(models.Model):
    prefix = models.CharField("Префикс", max_length=16)
    year = models.PositiveSmallIntegerField("Год")
    current_value = models.PositiveIntegerField("Текущее значение", default=0)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        ordering = ["prefix", "year"]
        verbose_name = "Счетчик документов"
        verbose_name_plural = "Счетчики документов"
        constraints = [
            models.UniqueConstraint(fields=["prefix", "year"], name="uniq_document_sequence_prefix_year"),
        ]

    def __str__(self) -> str:
        return f"{self.prefix}-{self.year}: {self.current_value}"


class UiThemeSettings(models.Model):
    name = models.CharField("Название", max_length=120, default="1C Light Blue")
    is_active = models.BooleanField("Активна", default=True)
    primary_color = models.CharField("Основной синий", max_length=7, default="#1b75d0", validators=[hex_color_validator])
    primary_hover = models.CharField("Синий при наведении", max_length=7, default="#155fa9", validators=[hex_color_validator])
    primary_soft = models.CharField("Светло-голубой акцент", max_length=7, default="#e7f2ff", validators=[hex_color_validator])
    background_color = models.CharField("Фон приложения", max_length=7, default="#f3f7fc", validators=[hex_color_validator])
    surface_color = models.CharField("Фон рабочей области", max_length=7, default="#ffffff", validators=[hex_color_validator])
    sidebar_color = models.CharField("Фон бокового меню", max_length=7, default="#eef5fc", validators=[hex_color_validator])
    border_color = models.CharField("Линии и границы", max_length=7, default="#d7e4f2", validators=[hex_color_validator])
    text_color = models.CharField("Основной текст", max_length=7, default="#20242a", validators=[hex_color_validator])
    muted_text_color = models.CharField("Вторичный текст", max_length=7, default="#6d7785", validators=[hex_color_validator])
    warning_color = models.CharField("Предупреждение", max_length=7, default="#ffce3a", validators=[hex_color_validator])
    danger_color = models.CharField("Ошибка", max_length=7, default="#d92d20", validators=[hex_color_validator])
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        verbose_name = "Настройка палитры интерфейса"
        verbose_name_plural = "Настройки палитры интерфейса"

    def __str__(self) -> str:
        return self.name

    @classmethod
    def default(cls):
        return cls()

    def as_css_variables(self) -> dict[str, str]:
        return {
            "--app-primary": self.primary_color,
            "--app-primary-hover": self.primary_hover,
            "--app-primary-soft": self.primary_soft,
            "--app-bg": self.background_color,
            "--app-surface": self.surface_color,
            "--app-sidebar": self.sidebar_color,
            "--app-border": self.border_color,
            "--app-text": self.text_color,
            "--app-muted": self.muted_text_color,
            "--app-warning": self.warning_color,
            "--app-danger": self.danger_color,
        }

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_active:
            UiThemeSettings.objects.exclude(pk=self.pk).update(is_active=False)


class UserProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name="Пользователь",
        on_delete=models.CASCADE,
        related_name="profile",
    )
    role = models.CharField("Роль", max_length=32, choices=UserRole.choices)
    department = models.ForeignKey(
        Department,
        verbose_name="ЦФО",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    is_app_access_enabled = models.BooleanField("Доступ к системе включен", default=True)
    theme_mode = models.CharField("Тема интерфейса", max_length=16, choices=UiThemeMode.choices, default=UiThemeMode.LIGHT)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        verbose_name = "Профиль пользователя"
        verbose_name_plural = "Профили пользователей"

    def __str__(self) -> str:
        return f"{self.user.username} · {self.get_role_display()}"

    @property
    def can_access_app(self) -> bool:
        return self.is_app_access_enabled and self.role != UserRole.ACCOUNTANT


class IntegrationRequest(models.Model):
    number = models.CharField("Номер заявки", max_length=32, unique=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Инициатор",
        on_delete=models.PROTECT,
        related_name="integration_requests",
    )
    integration_name = models.CharField("Название интеграции", max_length=120)
    target_system = models.CharField("Целевая система", max_length=120)
    description = models.TextField("Описание потребности")
    status = models.CharField(
        "Статус",
        max_length=20,
        choices=IntegrationRequestStatus.choices,
        default=IntegrationRequestStatus.NEW,
    )
    assigned_admin = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Ответственный администратор",
        on_delete=models.PROTECT,
        related_name="assigned_integration_requests",
        null=True,
        blank=True,
    )
    admin_comment = models.TextField("Комментарий администратора", blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Заявка на интеграцию"
        verbose_name_plural = "Заявки на интеграцию"

    def __str__(self) -> str:
        return f"{self.number} · {self.integration_name}"


class AuditLog(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Пользователь",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    action = models.CharField("Действие", max_length=32, choices=AuditAction.choices)
    path = models.CharField("Путь", max_length=255, blank=True)
    method = models.CharField("Метод", max_length=12, blank=True)
    status_code = models.PositiveSmallIntegerField("HTTP-статус", null=True, blank=True)
    object_type = models.CharField("Тип объекта", max_length=120, blank=True)
    object_id = models.CharField("ID объекта", max_length=120, blank=True)
    message = models.TextField("Сообщение", blank=True)
    ip_address = models.GenericIPAddressField("IP-адрес", null=True, blank=True)
    user_agent = models.CharField("User-Agent", max_length=255, blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Журнал действия"
        verbose_name_plural = "Журнал действий"
        indexes = [
            # Для timeline кейса финансирования: ищем по object_type='FundingCase' + object_id
            models.Index(fields=["object_type", "object_id"], name="audit_object_idx"),
        ]

    def __str__(self) -> str:
        actor = self.user.username if self.user else "anonymous"
        return f"{self.get_action_display()} · {actor} · {self.path}"


class Notification(models.Model):
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Получатель",
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    kind = models.CharField("Тип", max_length=64, choices=NotificationKind.choices)
    title = models.CharField("Заголовок", max_length=200)
    text = models.CharField("Текст", max_length=500, blank=True)
    link = models.CharField("Ссылка", max_length=255, blank=True)
    payload_object_type = models.CharField("Тип объекта", max_length=120, blank=True)
    payload_object_id = models.CharField("ID объекта", max_length=120, blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    read_at = models.DateTimeField("Прочитано", null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "read_at"]),
            models.Index(fields=["-created_at"]),
        ]
        verbose_name = "Уведомление"
        verbose_name_plural = "Уведомления"

    def __str__(self) -> str:
        return f"{self.get_kind_display()} · {self.recipient.username}"

    @property
    def is_read(self) -> bool:
        return self.read_at is not None
