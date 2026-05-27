"""Договоры и дополнительные соглашения."""

from django.db import models

from .enums import ContractKind, IntegrationTrackedModel
from .nsi import Counterparty, Currency


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

