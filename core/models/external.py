"""Внешний контур: документы оплаты во внешней (mock) 1С:БП."""

from django.conf import settings
from django.db import models

from .enums import ExternalPaymentStatus
from .nsi import Currency
from .contracts import Contract
from .payment_facts import PaymentFact


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

