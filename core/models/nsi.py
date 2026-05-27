"""Справочники НСИ: компании, ЦФО, валюты, статьи ДДС, контрагенты, номенклатура."""

from django.db import models

from .enums import IntegrationTrackedModel


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
