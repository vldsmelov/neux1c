from django.core.validators import RegexValidator
from django.db import models
from django.conf import settings
from django.utils import timezone


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


class AuditAction(models.TextChoices):
    VIEW = "view", "Просмотр"
    CREATE = "create", "Создание"
    UPDATE = "update", "Редактирование"
    DELETE = "delete", "Удаление"
    APPROVE = "approve", "Утверждение"
    PAY = "pay", "Оплата"
    LOGIN = "login", "Вход"
    DENIED = "denied", "Отказ доступа"


class ExternalPaymentStatus(models.TextChoices):
    DRAFT = "draft", "Черновик"
    POSTED = "posted", "Проведен"


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


class Currency(IntegrationTrackedModel):
    code = models.CharField("Код", max_length=3, unique=True)
    name = models.CharField("Наименование", max_length=120)

    class Meta:
        ordering = ["code"]
        verbose_name = "Валюта"
        verbose_name_plural = "Валюты"

    def __str__(self) -> str:
        return self.code


class Organization(IntegrationTrackedModel):
    name = models.CharField("Наименование", max_length=255, unique=True)
    inn = models.CharField("ИНН", max_length=12, blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Организация"
        verbose_name_plural = "Организации"

    def __str__(self) -> str:
        return self.name


class Department(IntegrationTrackedModel):
    name = models.CharField("Наименование", max_length=255)
    code = models.CharField("Код", max_length=32, unique=True)

    class Meta:
        ordering = ["code"]
        verbose_name = "ЦФО"
        verbose_name_plural = "ЦФО"

    def __str__(self) -> str:
        return f"{self.code} {self.name}"


class CashFlowArticle(IntegrationTrackedModel):
    code = models.CharField("Код", max_length=64, unique=True)
    name = models.CharField("Наименование", max_length=255)
    exists_in_one_c = models.BooleanField("Есть в 1С:БП", default=True)
    is_internal_turnover = models.BooleanField("ВГО", default=False)

    class Meta:
        ordering = ["code"]
        verbose_name = "Статья ДДС"
        verbose_name_plural = "Статьи ДДС"

    def __str__(self) -> str:
        return f"{self.code} {self.name}"


class Counterparty(IntegrationTrackedModel):
    name = models.CharField("Наименование", max_length=255)
    inn = models.CharField("ИНН", max_length=12, blank=True)
    can_create_manually = models.BooleanField("Можно создавать вручную", default=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Контрагент"
        verbose_name_plural = "Контрагенты"
        constraints = [
            models.UniqueConstraint(fields=["name", "inn"], name="uniq_counterparty_name_inn"),
        ]

    def __str__(self) -> str:
        return self.name


class Nomenclature(IntegrationTrackedModel):
    name = models.CharField("Наименование", max_length=255)
    code = models.CharField("Код", max_length=64, unique=True)
    can_create_manually = models.BooleanField("Можно создавать вручную", default=True)

    class Meta:
        ordering = ["code"]
        verbose_name = "Номенклатура"
        verbose_name_plural = "Номенклатура"

    def __str__(self) -> str:
        return f"{self.code} {self.name}"


class Contract(IntegrationTrackedModel):
    number = models.CharField("Номер", max_length=64)
    date = models.DateField("Дата")
    name = models.CharField("Наименование", max_length=255)
    kind = models.CharField("Тип договора", max_length=32, choices=ContractKind.choices)
    counterparty = models.ForeignKey(
        Counterparty,
        verbose_name="Контрагент",
        on_delete=models.PROTECT,
        related_name="contracts",
    )
    parent_customer_contract = models.ForeignKey(
        "self",
        verbose_name="Доходный договор",
        on_delete=models.PROTECT,
        related_name="supplier_contracts",
        null=True,
        blank=True,
    )
    currency = models.ForeignKey(Currency, verbose_name="Валюта", on_delete=models.PROTECT)
    amount = models.DecimalField("Сумма", max_digits=16, decimal_places=2)
    manual_exchange_rate = models.DecimalField(
        "Ручной курс валюты",
        max_digits=12,
        decimal_places=4,
        default=1,
    )

    class Meta:
        ordering = ["date", "number"]
        verbose_name = "Договор"
        verbose_name_plural = "Договоры"
        constraints = [
            models.UniqueConstraint(fields=["kind", "number", "date"], name="uniq_contract_kind_number_date"),
        ]

    def __str__(self) -> str:
        return f"{self.number} от {self.date:%d.%m.%Y}"

    @property
    def reserved_amount(self):
        agreements_sum = self.additional_agreements.aggregate(total=models.Sum("amount"))["total"] or 0
        return self.amount + agreements_sum


class AdditionalAgreement(IntegrationTrackedModel):
    contract = models.ForeignKey(
        Contract,
        verbose_name="Договор",
        on_delete=models.CASCADE,
        related_name="additional_agreements",
    )
    number = models.CharField("Номер", max_length=64)
    date = models.DateField("Дата")
    amount = models.DecimalField("Сумма", max_digits=16, decimal_places=2)
    currency = models.ForeignKey(Currency, verbose_name="Валюта", on_delete=models.PROTECT)

    class Meta:
        ordering = ["date", "number"]
        verbose_name = "Дополнительное соглашение"
        verbose_name_plural = "Дополнительные соглашения"
        constraints = [
            models.UniqueConstraint(fields=["contract", "number", "date"], name="uniq_agreement_contract_number_date"),
        ]

    def __str__(self) -> str:
        return f"{self.number} к {self.contract.number}"


class PaymentFact(IntegrationTrackedModel):
    organization = models.ForeignKey(Organization, verbose_name="Организация", on_delete=models.PROTECT)
    date = models.DateField("Дата")
    account = models.CharField("Счет учета", max_length=2)
    direction = models.CharField("Направление", max_length=16, choices=PaymentDirection.choices)
    accounting_kind = models.CharField("Вид учета", max_length=8, choices=AccountingKind.choices)
    article = models.ForeignKey(CashFlowArticle, verbose_name="Статья ДДС", on_delete=models.PROTECT)
    counterparty = models.ForeignKey(Counterparty, verbose_name="Контрагент", on_delete=models.PROTECT)
    contract = models.ForeignKey(
        Contract,
        verbose_name="Договор",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    amount = models.DecimalField("Сумма", max_digits=16, decimal_places=2)
    currency = models.ForeignKey(Currency, verbose_name="Валюта", on_delete=models.PROTECT)
    comment = models.CharField("Комментарий", max_length=255, blank=True)

    class Meta:
        ordering = ["date", "external_id"]
        verbose_name = "Факт оплаты"
        verbose_name_plural = "Факты оплат"
        constraints = [
            models.UniqueConstraint(fields=["external_id", "accounting_kind"], name="uniq_payment_fact_external_accounting"),
        ]

    def __str__(self) -> str:
        return f"{self.get_accounting_kind_display()} {self.date:%d.%m.%Y} {self.amount}"


class ExternalPaymentDocument(models.Model):
    number = models.CharField("Номер", max_length=32, unique=True)
    contract = models.ForeignKey(
        Contract,
        verbose_name="Договор",
        on_delete=models.PROTECT,
        related_name="external_payments",
    )
    accountant = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Бухгалтер",
        on_delete=models.PROTECT,
        related_name="external_payments",
    )
    payment_date = models.DateField("Дата оплаты")
    amount = models.DecimalField("Сумма", max_digits=16, decimal_places=2)
    currency = models.ForeignKey(Currency, verbose_name="Валюта", on_delete=models.PROTECT)
    status = models.CharField(
        "Статус",
        max_length=16,
        choices=ExternalPaymentStatus.choices,
        default=ExternalPaymentStatus.POSTED,
    )
    payment_fact_bu = models.ForeignKey(
        PaymentFact,
        verbose_name="Факт БУ",
        on_delete=models.SET_NULL,
        related_name="+",
        null=True,
        blank=True,
    )
    payment_fact_nu = models.ForeignKey(
        PaymentFact,
        verbose_name="Факт НУ",
        on_delete=models.SET_NULL,
        related_name="+",
        null=True,
        blank=True,
    )
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)

    class Meta:
        ordering = ["-payment_date", "-created_at"]
        verbose_name = "Внешняя оплата договора"
        verbose_name_plural = "Внешние оплаты договоров"

    def __str__(self) -> str:
        return f"{self.number} · {self.contract.number}"


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

    def __str__(self) -> str:
        actor = self.user.username if self.user else "anonymous"
        return f"{self.get_action_display()} · {actor} · {self.path}"
