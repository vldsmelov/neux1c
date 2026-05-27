"""Факт оплат (БУ/НУ), настройки контроля факта и версионируемые корректировки."""

from django.conf import settings
from django.db import models

from .enums import AccountingKind, IntegrationTrackedModel, PaymentDirection
from .nsi import CashFlowArticle, Counterparty, Currency, Organization
from .contracts import Contract


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


class PaymentFactControlSettings(models.Model):
    name = models.CharField("Название", max_length=120, default="Контроль корректировок факта")
    is_active = models.BooleanField("Активна", default=True)
    closed_through = models.DateField("Период закрыт по", null=True, blank=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        verbose_name = "Настройка закрытия периода по факту оплат"
        verbose_name_plural = "Настройки закрытия периода по факту оплат"

    def __str__(self) -> str:
        return self.name

    @classmethod
    def active_or_default(cls):
        active = cls.objects.filter(is_active=True).first()
        if active is not None:
            return active
        return cls()

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_active:
            PaymentFactControlSettings.objects.exclude(pk=self.pk).update(is_active=False)


class PaymentFactAdjustment(models.Model):
    payment_fact = models.ForeignKey(
        PaymentFact,
        verbose_name="Факт оплаты",
        on_delete=models.CASCADE,
        related_name="adjustments",
    )
    version = models.PositiveIntegerField("Версия")
    previous_amount = models.DecimalField("Сумма до корректировки", max_digits=16, decimal_places=2)
    new_amount = models.DecimalField("Сумма после корректировки", max_digits=16, decimal_places=2)
    previous_comment = models.CharField("Комментарий до корректировки", max_length=255, blank=True)
    new_comment = models.CharField("Комментарий после корректировки", max_length=255, blank=True)
    reason = models.TextField("Причина корректировки")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Автор корректировки",
        on_delete=models.PROTECT,
        related_name="payment_fact_adjustments",
    )
    created_at = models.DateTimeField("Создано", auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Корректировка факта оплаты"
        verbose_name_plural = "Корректировки факта оплаты"
        constraints = [
            models.UniqueConstraint(fields=["payment_fact", "version"], name="uniq_payment_fact_adjustment_version"),
        ]

    def __str__(self) -> str:
        return f"{self.payment_fact_id} · v{self.version}"

