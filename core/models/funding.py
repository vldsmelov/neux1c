"""Кейсы финансирования: связка доходных договоров, расходных, займов и фактов
в одну сделку с единым сальдо.

Пример: материнская компания спустила ТЗ на закупку стройматериалов на 2 000 000,
поставщик берёт 2 500 000, дефицит 500 000 закрываем займом у третьей фирмы,
потом мама возвращает 500 000 + проценты, гасим заём. Всё это — один кейс
финансирования; на карточке кейса видно текущее сальдо и кому что должны.
"""

from django.conf import settings
from django.db import models

from .contracts import Contract
from .enums import FundingCaseStatus
from .nsi import Organization


class FundingCase(models.Model):
    code = models.CharField("Код", max_length=32, unique=True)
    name = models.CharField("Название кейса", max_length=255)
    organization = models.ForeignKey(
        Organization,
        verbose_name="Компания",
        on_delete=models.PROTECT,
        related_name="funding_cases",
    )
    description = models.TextField("Описание / Цель", blank=True)
    status = models.CharField(
        "Статус",
        max_length=24,
        choices=FundingCaseStatus.choices,
        default=FundingCaseStatus.ACTIVE,
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Ответственный",
        on_delete=models.PROTECT,
        related_name="owned_funding_cases",
        null=True,
        blank=True,
    )
    opened_at = models.DateField("Дата открытия кейса", auto_now_add=True)
    closed_at = models.DateField("Дата закрытия", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-opened_at", "code"]
        verbose_name = "Кейс финансирования"
        verbose_name_plural = "Кейсы финансирования"

    def __str__(self) -> str:
        return f"{self.code} · {self.name}"


def _gen_webhook_secret() -> str:
    import secrets as _secrets
    return _secrets.token_urlsafe(32)


class PlanDriver(models.Model):
    """Драйвер плана: показатель с помесячными значениями.

    Driver-based planning (Anaplan / Workday Adaptive стиль): план
    формируется не вводом одной суммы, а как функция нескольких
    драйверов. Например, Revenue = Volume × Price × Seasonality;
    при изменении одного драйвера весь план пересчитывается.

    Типы:
    * volume — объём (штук, тонн, услуг)
    * price — цена за единицу
    * multiplier — мультипликатор (сезонность, скидка, growth rate)
    * cost_per_unit — переменная себестоимость
    * fixed — фиксированная сумма по месяцам

    monthly_values хранится как JSON list из 12 чисел (январь..декабрь).
    """

    KIND_VOLUME = "volume"
    KIND_PRICE = "price"
    KIND_MULTIPLIER = "multiplier"
    KIND_COST = "cost_per_unit"
    KIND_FIXED = "fixed"
    KIND_CHOICES = [
        (KIND_VOLUME, "Объём"),
        (KIND_PRICE, "Цена за единицу"),
        (KIND_MULTIPLIER, "Мультипликатор / коэффициент"),
        (KIND_COST, "Переменная себестоимость"),
        (KIND_FIXED, "Фиксированная сумма"),
    ]

    name = models.CharField("Название", max_length=200)
    code = models.CharField("Код", max_length=64, blank=True, help_text="Короткое имя для формул")
    organization = models.ForeignKey(
        Organization,
        verbose_name="Организация",
        on_delete=models.CASCADE,
        related_name="plan_drivers",
    )
    year = models.PositiveSmallIntegerField("Год планирования")
    kind = models.CharField(
        "Тип драйвера",
        max_length=20,
        choices=KIND_CHOICES,
        default=KIND_VOLUME,
    )
    unit = models.CharField("Единица", max_length=32, blank=True, help_text="шт., т., ₽/шт., %")
    monthly_values = models.JSONField(
        "Помесячные значения",
        default=list,
        help_text="JSON-list из 12 чисел: январь..декабрь",
    )
    comment = models.TextField("Комментарий", blank=True)
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Создал",
        on_delete=models.PROTECT,
        related_name="plan_drivers_created",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["organization__name", "-year", "kind", "name"]
        verbose_name = "Драйвер плана"
        verbose_name_plural = "Драйверы плана"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "year", "code"],
                name="uniq_plan_driver_org_year_code",
                condition=models.Q(code__gt=""),
            ),
        ]

    def __str__(self) -> str:
        code_part = f" [{self.code}]" if self.code else ""
        return f"{self.name}{code_part} · {self.year}"

    def annual_total(self):
        from decimal import Decimal
        if not isinstance(self.monthly_values, list):
            return Decimal("0")
        try:
            return sum((Decimal(str(v)) for v in self.monthly_values), Decimal("0"))
        except Exception:
            return Decimal("0")

    def annual_average(self):
        from decimal import Decimal
        vals = self.monthly_values if isinstance(self.monthly_values, list) else []
        if not vals:
            return Decimal("0")
        try:
            total = sum(Decimal(str(v)) for v in vals)
            return total / Decimal(len(vals))
        except Exception:
            return Decimal("0")


class ApprovalChain(models.Model):
    """Настраиваемая цепочка согласования для платёжных заявок.

    Заменяет жёсткую 2-stage эскалацию по amount threshold на гибкую
    N-stage цепочку. Для каждой организации можно настроить несколько
    цепочек с разными условиями активации (вид документа, диапазон сумм).

    Stages хранятся как JSON list, каждый stage:
    {
        "order": 1,
        "approver_username": "manager",
        "min_amount": 0,
        "max_amount": 1000000  # null = без верхней границы
    }

    Алгоритм применения:
    1. Когда заявка отправляется — ищем applicable chain по
       (organization, applies_to_kind, amount_rub).
    2. Берём первый stage, ставим approver, статус = pending_approval.
    3. При approve — продвигаем на следующий stage; если был последний —
       финализируем как APPROVED.
    4. При reject на любом stage — сразу REJECTED.
    """

    APPLIES_PAYMENT_REQUEST = "payment_request"
    APPLIES_BUDGET_PLAN = "budget_plan"
    APPLIES_CHOICES = [
        (APPLIES_PAYMENT_REQUEST, "Заявка на оплату"),
        (APPLIES_BUDGET_PLAN, "Бюджет / лимит"),
    ]

    name = models.CharField("Название", max_length=200)
    organization = models.ForeignKey(
        Organization,
        verbose_name="Организация",
        on_delete=models.CASCADE,
        related_name="approval_chains",
    )
    applies_to_kind = models.CharField(
        "Применяется к",
        max_length=32,
        choices=APPLIES_CHOICES,
        default=APPLIES_PAYMENT_REQUEST,
    )
    min_amount_rub = models.DecimalField(
        "Сумма от, RUB",
        max_digits=16,
        decimal_places=2,
        default=0,
        help_text="Документы с amount_rub ≥ этого значения попадают в цепочку",
    )
    max_amount_rub = models.DecimalField(
        "Сумма до, RUB",
        max_digits=16,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Документы с amount_rub ≤ этого значения. Пусто = без верхней границы",
    )
    stages = models.JSONField(
        "Этапы",
        default=list,
        help_text='JSON-список: [{"order": 1, "approver_username": "manager", "comment": "..."}]',
    )
    is_active = models.BooleanField("Активна", default=True)
    comment = models.TextField("Комментарий", blank=True)
    created_at = models.DateTimeField("Создана", auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Создал",
        on_delete=models.PROTECT,
        related_name="approval_chains_created",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["organization__name", "min_amount_rub"]
        verbose_name = "Цепочка согласования"
        verbose_name_plural = "Цепочки согласования"

    def __str__(self) -> str:
        return f"{self.name} · {self.organization.name}"

    def stages_count(self) -> int:
        try:
            return len(self.stages or [])
        except (TypeError, AttributeError):
            return 0


class WebhookSubscription(models.Model):
    """Outbound webhook: внешняя система регистрирует URL, на который
    NE UX будет отправлять POST с JSON-payload при наступлении событий.

    События v1:
    * funding_case.status_changed — статус кейса изменился
    * funding_case.created — создан новый кейс
    * payment_fact.large — записан факт сверх порога
    * loan.maturity_due_soon — заём близок к сроку возврата

    Для безопасности — HMAC-SHA256 подпись в заголовке X-NEUX-Signature
    по secret, который генерируется при создании subscription.
    """

    EVENT_CASE_STATUS = "funding_case.status_changed"
    EVENT_CASE_CREATED = "funding_case.created"
    EVENT_LARGE_FACT = "payment_fact.large"
    EVENT_LOAN_MATURITY = "loan.maturity_due_soon"
    EVENT_CHOICES = [
        (EVENT_CASE_STATUS, "Смена статуса кейса"),
        (EVENT_CASE_CREATED, "Создание кейса"),
        (EVENT_LARGE_FACT, "Крупный факт оплаты"),
        (EVENT_LOAN_MATURITY, "Приближение срока возврата займа"),
    ]

    name = models.CharField("Название", max_length=120)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Владелец",
        on_delete=models.CASCADE,
        related_name="webhooks",
    )
    event_type = models.CharField("Событие", max_length=64, choices=EVENT_CHOICES)
    target_url = models.URLField("URL-приёмник", max_length=500)
    secret = models.CharField(
        "Секрет для HMAC",
        max_length=128,
        default=_gen_webhook_secret,
        help_text="Используется для подписи заголовка X-NEUX-Signature (HMAC-SHA256)",
    )
    is_active = models.BooleanField("Активен", default=True)
    last_fired_at = models.DateTimeField("Последняя отправка", null=True, blank=True)
    last_status_code = models.IntegerField("Последний HTTP-код ответа", null=True, blank=True)
    failure_count = models.IntegerField("Подряд неудачных", default=0)
    created_at = models.DateTimeField("Создан", auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Webhook-подписка"
        verbose_name_plural = "Webhook-подписки"

    def __str__(self) -> str:
        return f"{self.name} · {self.get_event_type_display()}"


class ApiToken(models.Model):
    """API-токен для интеграций. Передаётся в заголовке Authorization
    как `Token <uuid>`. У пользователя может быть несколько токенов
    (например, для разных интеграций — CRM, BI, бухгалтерия)."""

    import uuid as _uuid

    token = models.UUIDField("Токен", default=_uuid.uuid4, unique=True, editable=False)
    name = models.CharField("Название", max_length=120, help_text="Например, «CRM Bitrix integration» или «BI Power BI»")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Владелец",
        on_delete=models.CASCADE,
        related_name="api_tokens",
        help_text="API-запросы выполняются от имени этого пользователя — наследуют его права RBAC",
    )
    is_active = models.BooleanField("Активен", default=True)
    last_used_at = models.DateTimeField("Последнее использование", null=True, blank=True)
    created_at = models.DateTimeField("Создан", auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "API-токен"
        verbose_name_plural = "API-токены"

    def __str__(self) -> str:
        return f"{self.name} ({self.user.username})"


class AllocationRule(models.Model):
    """Правило распределения общих расходов между ЦФО.

    Enterprise-функция: shared costs (аренда офиса, электричество,
    интернет, центральная бухгалтерия) изначально лежат на одном ЦФО
    («Administration»), но для реальной себестоимости их нужно
    распределить по операционным ЦФО по правилам:
    * percent — фиксированные проценты (Administration 0%, Sales 40%,
      Production 60%);
    * equal — поровну между указанными ЦФО;
    * driver — пропорционально другому показателю (количество сотрудников,
      выручка, метраж) — пока заглушка.

    Распределение применяется к фактам по source_article (например,
    DDS-020 «Аренда») и создаёт виртуальное представление «как
    выглядел бы факт после распределения».
    """

    METHOD_PERCENT = "percent"
    METHOD_EQUAL = "equal"
    METHOD_CHOICES = [
        (METHOD_PERCENT, "Фиксированные проценты"),
        (METHOD_EQUAL, "Равные доли"),
    ]

    name = models.CharField("Название", max_length=200)
    organization = models.ForeignKey(
        Organization,
        verbose_name="Организация",
        on_delete=models.CASCADE,
        related_name="allocation_rules",
    )
    source_article = models.ForeignKey(
        "core.CashFlowArticle",
        verbose_name="Статья-источник",
        on_delete=models.PROTECT,
        related_name="allocation_rules",
        help_text="Факты по этой статье будут распределены",
    )
    method = models.CharField(
        "Метод распределения",
        max_length=16,
        choices=METHOD_CHOICES,
        default=METHOD_PERCENT,
    )
    target_pct = models.JSONField(
        "Целевые доли (ЦФО → процент)",
        default=dict,
        help_text='Формат: {"<department_id>": <процент>}, сумма = 100.',
    )
    is_active = models.BooleanField("Активно", default=True)
    comment = models.TextField("Комментарий", blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Создал",
        on_delete=models.PROTECT,
        related_name="allocation_rules_created",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["organization__name", "name"]
        verbose_name = "Правило распределения"
        verbose_name_plural = "Правила распределения"

    def __str__(self) -> str:
        return f"{self.name} → {self.source_article.code}"

    def total_pct(self) -> int:
        try:
            return sum(int(v) for v in (self.target_pct or {}).values())
        except (TypeError, ValueError):
            return 0


class ExchangeRate(models.Model):
    """Курс валюты к базовой (RUB) на конкретную дату.

    Enterprise-функция: позволяет пересчитывать валютные операции по
    курсу на дату документа, а не на сегодня. Основа для FX revaluation
    и корректного план-факта в multi-currency холдингах.

    Источник может быть:
    * `manual` — ввёл финансист вручную (например, банковский курс)
    * `cbr` — выгружено из ЦБ РФ (когда реализуем интеграцию)
    * `internal` — внутригрупповой курс из учётной политики

    Курс хранится как «1 единица валюты = N RUB», максимум 6 знаков
    после запятой (хватает для криптовалют тоже).
    """

    SOURCE_MANUAL = "manual"
    SOURCE_CBR = "cbr"
    SOURCE_INTERNAL = "internal"
    SOURCE_CHOICES = [
        (SOURCE_MANUAL, "Введён вручную"),
        (SOURCE_CBR, "ЦБ РФ"),
        (SOURCE_INTERNAL, "Внутренний (учётная политика)"),
    ]

    currency = models.ForeignKey(
        "core.Currency",
        verbose_name="Валюта",
        on_delete=models.PROTECT,
        related_name="exchange_rates",
    )
    rate_date = models.DateField("Дата курса")
    rate_to_rub = models.DecimalField(
        "Курс к RUB",
        max_digits=14,
        decimal_places=6,
        help_text="Сколько RUB за единицу валюты на эту дату",
    )
    source = models.CharField(
        "Источник",
        max_length=16,
        choices=SOURCE_CHOICES,
        default=SOURCE_MANUAL,
    )
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кто добавил",
        on_delete=models.PROTECT,
        related_name="exchange_rates_created",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["-rate_date", "currency__code"]
        verbose_name = "Курс валюты"
        verbose_name_plural = "Курсы валют"
        constraints = [
            models.UniqueConstraint(
                fields=["currency", "rate_date", "source"],
                name="uniq_exchange_rate_curr_date_src",
            ),
        ]
        indexes = [
            models.Index(fields=["currency", "-rate_date"], name="exchange_rate_lookup_idx"),
        ]

    def __str__(self) -> str:
        return f"1 {self.currency.code} = {self.rate_to_rub} ₽ на {self.rate_date:%d.%m.%Y}"


class AccountingPeriodLock(models.Model):
    """Закрытие учётного периода (Period Close) — enterprise-уровневая
    функция финансового контроля. После закрытия месяца:
    * нельзя создать факт оплаты с датой в закрытом периоде;
    * нельзя редактировать существующий факт закрытого периода;
    * нельзя добавить корректировку факта в закрытом периоде;
    * требуется разблокировка финдиректором/админом для исправлений.

    Закрытие выполняется по (organization, year, month) — каждая
    компания закрывает свои месяцы независимо.
    """

    organization = models.ForeignKey(
        Organization,
        verbose_name="Организация",
        on_delete=models.CASCADE,
        related_name="period_locks",
    )
    year = models.PositiveSmallIntegerField("Год")
    month = models.PositiveSmallIntegerField("Месяц (1-12)")
    locked_at = models.DateTimeField("Дата закрытия", auto_now_add=True)
    locked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кто закрыл",
        on_delete=models.PROTECT,
        related_name="period_locks",
    )
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    unlocked_at = models.DateTimeField("Дата открытия для правок", null=True, blank=True)
    unlocked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кто открыл",
        on_delete=models.PROTECT,
        related_name="period_unlocks",
        null=True,
        blank=True,
    )
    unlock_reason = models.CharField("Причина повторного открытия", max_length=255, blank=True)

    class Meta:
        ordering = ["-year", "-month", "organization__name"]
        verbose_name = "Закрытие учётного периода"
        verbose_name_plural = "Закрытия учётных периодов"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "year", "month"],
                name="uniq_period_lock_org_year_month",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "year", "month"], name="period_lock_lookup_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.organization.name} · {self.year}-{self.month:02d}"

    @property
    def is_active(self) -> bool:
        return self.unlocked_at is None


class LoanScheduleLine(models.Model):
    """Строка графика платежей по займу: одна строка = один период
    (обычно месяц). Принципиальная конструкция аннуитета: PMT константный,
    проценты считаются от остатка тела, оставшееся идёт в тело."""
    loan_contract = models.ForeignKey(
        Contract,
        verbose_name="Договор займа",
        on_delete=models.CASCADE,
        related_name="schedule_lines",
    )
    period = models.PositiveSmallIntegerField("Номер периода")
    due_date = models.DateField("Дата платежа")
    principal_due = models.DecimalField("Тело, к оплате", max_digits=16, decimal_places=2)
    interest_due = models.DecimalField("Проценты, к оплате", max_digits=16, decimal_places=2)
    balance_after = models.DecimalField("Остаток тела после периода", max_digits=16, decimal_places=2)
    paid_amount = models.DecimalField("Оплачено по этой строке", max_digits=16, decimal_places=2, default=0)
    paid_at = models.DateField("Дата фактической оплаты", null=True, blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)

    class Meta:
        ordering = ["loan_contract_id", "period"]
        verbose_name = "Строка графика платежей по займу"
        verbose_name_plural = "Строки графика платежей по займу"
        constraints = [
            models.UniqueConstraint(fields=["loan_contract", "period"], name="uniq_loan_schedule_period"),
        ]
        indexes = [
            models.Index(fields=["loan_contract", "due_date"]),
        ]

    def __str__(self) -> str:
        return f"{self.loan_contract.number} · период {self.period}"

    @property
    def total_due(self):
        return self.principal_due + self.interest_due

    @property
    def is_paid(self) -> bool:
        return self.paid_at is not None and self.paid_amount >= self.total_due

    @property
    def is_overdue(self) -> bool:
        if self.is_paid:
            return False
        from django.utils import timezone as _tz
        return self.due_date < _tz.localdate()
