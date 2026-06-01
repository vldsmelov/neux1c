"""Справочники НСИ: компании, ЦФО, валюты, статьи ДДС, контрагенты, номенклатура."""

from django.db import models

from .enums import CashFlowDirection, IntegrationTrackedModel


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
    is_holding_member = models.BooleanField(
        "Член холдинга",
        default=True,
        help_text="Если включено — компания входит в периметр консолидированной отчётности. Сделки с другими членами холдинга считаются ВГО и устраняются при консолидации.",
    )
    escalation_threshold_rub = models.DecimalField(
        "Порог эскалации, RUB",
        max_digits=16,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Заявки на сумму ≥ порога после первого согласования уходят на финальное согласование",
    )
    secondary_approver = models.ForeignKey(
        "auth.User",
        verbose_name="Финальный согласующий",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="secondary_approver_for_organizations",
    )

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
    parent = models.ForeignKey(
        "self",
        verbose_name="Родительская статья (группа)",
        on_delete=models.PROTECT,
        related_name="children",
        null=True,
        blank=True,
        help_text="Группировка статей: операционные / инвестиционные / финансовые и т.д.",
    )
    direction = models.CharField(
        "Направление потока",
        max_length=16,
        choices=CashFlowDirection.choices,
        default=CashFlowDirection.OUTFLOW,
    )
    is_group = models.BooleanField(
        "Группа",
        default=False,
        help_text="True для статей-группировок; на группу нельзя списать деньги напрямую",
    )

    class Meta:
        ordering = ["code"]
        verbose_name = "Статья ДДС"
        verbose_name_plural = "Статьи ДДС"

    def __str__(self) -> str:
        return f"{self.code} {self.name}"

    @property
    def full_path(self) -> str:
        """Code path from root to this article, e.g. 'OP / DDS-010'."""
        if self.parent_id is None:
            return self.code
        parts: list[str] = []
        node = self
        depth = 0
        while node is not None and depth < 10:
            parts.append(node.code)
            node = node.parent
            depth += 1
        return " / ".join(reversed(parts))

    def descendant_ids(self) -> list[int]:
        """All article ids in the subtree rooted at this article (inclusive).

        Uses a simple iterative BFS; with shallow hierarchies (3-4 levels)
        and modest fan-out this is cheaper than recursive SQL.
        """
        ids: list[int] = [self.id]
        frontier = [self.id]
        while frontier:
            next_frontier = list(
                CashFlowArticle.objects.filter(parent_id__in=frontier).values_list("id", flat=True)
            )
            ids.extend(next_frontier)
            frontier = next_frontier
        return ids


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
