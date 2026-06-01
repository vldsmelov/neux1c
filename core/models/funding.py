"""Кейсы финансирования: связка доходных договоров, расходных, займов и фактов
в одну сделку с единым сальдо.

Пример: материнская компания спустила ТЗ на закупку стройматериалов на 2 000 000,
поставщик берёт 2 500 000, дефицит 500 000 закрываем займом у третьей фирмы,
потом мама возвращает 500 000 + проценты, гасим заём. Всё это — один кейс
финансирования; на карточке кейса видно текущее сальдо и кому что должны.
"""

from django.conf import settings
from django.db import models

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
