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
