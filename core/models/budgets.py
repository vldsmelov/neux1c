"""Бюджеты, лимиты, помесячная разбивка и корректировки лимитов."""

from django.conf import settings
from django.db import models
from django.utils import timezone

from .enums import BudgetPeriodicity, BudgetPlanStatus, BudgetScope, PlanningScenarioKind
from .nsi import CashFlowArticle, Currency, Department, Organization


class PlanningScenario(models.Model):
    """Сценарий планирования: версия плана (базовая / оптимистичная / своя).

    Все бюджеты и лимиты привязаны к сценарию. Активным (для отчётов
    по умолчанию) считается сценарий с is_baseline=True.
    """

    name = models.CharField("Название", max_length=120)
    year = models.PositiveSmallIntegerField("Год планирования")
    organization = models.ForeignKey(
        Organization,
        verbose_name="Компания",
        on_delete=models.CASCADE,
        related_name="planning_scenarios",
    )
    kind = models.CharField(
        "Тип",
        max_length=16,
        choices=PlanningScenarioKind.choices,
        default=PlanningScenarioKind.BASE,
    )
    is_baseline = models.BooleanField(
        "Базовый для компании/года",
        default=False,
        help_text="Только один сценарий компании в году может быть базовым; используется в отчётах по умолчанию",
    )
    comment = models.TextField("Комментарий", blank=True)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Автор",
        on_delete=models.PROTECT,
        related_name="planning_scenarios",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        ordering = ["-year", "organization__name", "name"]
        verbose_name = "Сценарий планирования"
        verbose_name_plural = "Сценарии планирования"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "year", "name"],
                name="uniq_scenario_org_year_name",
            ),
        ]

    def __str__(self) -> str:
        baseline = " · базовый" if self.is_baseline else ""
        return f"{self.name} ({self.year}, {self.organization.name}){baseline}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Гарантируем единственный baseline на (organization, year)
        if self.is_baseline:
            PlanningScenario.objects.filter(
                organization_id=self.organization_id,
                year=self.year,
            ).exclude(pk=self.pk).update(is_baseline=False)

    @classmethod
    def baseline_for(cls, organization, year):
        return cls.objects.filter(organization=organization, year=year, is_baseline=True).first()

    @classmethod
    def ensure_baseline(cls, organization, year, *, author=None):
        existing = cls.baseline_for(organization, year)
        if existing is not None:
            return existing
        return cls.objects.create(
            name="Базовый",
            year=year,
            organization=organization,
            kind=PlanningScenarioKind.BASE,
            is_baseline=True,
            author=author,
        )


class BudgetPlan(models.Model):
    number = models.CharField("Номер бюджета", max_length=32, unique=True)
    document_date = models.DateField("Дата документа", default=timezone.localdate)
    organization = models.ForeignKey(
        Organization,
        verbose_name="Компания",
        on_delete=models.PROTECT,
        related_name="budgets",
    )
    budget_year = models.PositiveSmallIntegerField("Период бюджета")
    scenario = models.ForeignKey(
        "core.PlanningScenario",
        verbose_name="Сценарий планирования",
        on_delete=models.PROTECT,
        related_name="budgets",
        null=True,
        blank=True,
    )
    periodicity = models.CharField(
        "Периодичность",
        max_length=16,
        choices=BudgetPeriodicity.choices,
        default=BudgetPeriodicity.YEAR,
    )
    scope = models.CharField(
        "Вид бюджета",
        max_length=32,
        choices=BudgetScope.choices,
        default=BudgetScope.OVERALL,
    )
    currency = models.ForeignKey(Currency, verbose_name="Валюта бюджета", on_delete=models.PROTECT)
    total_amount = models.DecimalField("Сумма бюджета", max_digits=16, decimal_places=2)
    comment = models.TextField("Комментарий", blank=True)
    status = models.CharField(
        "Статус",
        max_length=32,
        choices=BudgetPlanStatus.choices,
        default=BudgetPlanStatus.DRAFT,
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Автор",
        on_delete=models.PROTECT,
        related_name="budget_documents",
    )
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        ordering = ["-document_date", "-created_at"]
        verbose_name = "Бюджет"
        verbose_name_plural = "Бюджеты"

    def __str__(self) -> str:
        return f"{self.number} · {self.budget_year}"

    def save(self, *args, **kwargs):
        if self.organization_id is None:
            organization = Organization.objects.order_by("id").first()
            if organization is not None:
                self.organization = organization
        super().save(*args, **kwargs)


class BudgetDepartmentAllocation(models.Model):
    budget = models.ForeignKey(
        BudgetPlan,
        verbose_name="Бюджет",
        on_delete=models.CASCADE,
        related_name="department_allocations",
    )
    department = models.ForeignKey(Department, verbose_name="ЦФО", on_delete=models.PROTECT)
    amount = models.DecimalField("Сумма бюджета ЦФО", max_digits=16, decimal_places=2)

    class Meta:
        ordering = ["department__code"]
        verbose_name = "Строка бюджета по ЦФО"
        verbose_name_plural = "Строки бюджета по ЦФО"
        constraints = [
            models.UniqueConstraint(fields=["budget", "department"], name="uniq_budget_department_allocation"),
        ]

    def __str__(self) -> str:
        return f"{self.budget.number} · {self.department.code}"


class BudgetLimitPlan(models.Model):
    number = models.CharField("Номер документа", max_length=32, unique=True)
    document_date = models.DateField("Дата документа", default=timezone.localdate)
    organization = models.ForeignKey(
        Organization,
        verbose_name="Компания",
        on_delete=models.PROTECT,
        related_name="budget_limits",
    )
    budget = models.ForeignKey(
        BudgetPlan,
        verbose_name="Бюджет",
        on_delete=models.PROTECT,
        related_name="limits",
        null=True,
        blank=True,
    )
    planning_year = models.PositiveSmallIntegerField("Период планирования")
    scenario = models.ForeignKey(
        "core.PlanningScenario",
        verbose_name="Сценарий планирования",
        on_delete=models.PROTECT,
        related_name="limits",
        null=True,
        blank=True,
    )
    planning_horizon = models.PositiveSmallIntegerField("Горизонт планирования", default=1)
    department = models.ForeignKey(Department, verbose_name="ЦФО", on_delete=models.PROTECT)
    article = models.ForeignKey(CashFlowArticle, verbose_name="Статья ДДС", on_delete=models.PROTECT)
    currency = models.ForeignKey(Currency, verbose_name="Валюта плана", on_delete=models.PROTECT)
    annual_amount = models.DecimalField("Сумма плана", max_digits=16, decimal_places=2)
    comment = models.TextField("Комментарий", blank=True)
    status = models.CharField(
        "Статус",
        max_length=32,
        choices=BudgetPlanStatus.choices,
        default=BudgetPlanStatus.DRAFT,
    )
    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Утверждающий",
        on_delete=models.PROTECT,
        related_name="budget_plans_to_approve",
    )
    approved_at = models.DateTimeField("Дата утверждения", null=True, blank=True)
    version = models.PositiveIntegerField("Версия", default=1)
    correction_reason = models.CharField("Основание для корректировки", max_length=255, blank=True)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Автор",
        on_delete=models.PROTECT,
        related_name="budget_plans",
    )
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        ordering = ["-document_date", "-created_at"]
        verbose_name = "План / лимит"
        verbose_name_plural = "Планы / лимиты"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(planning_horizon__in=[1, 2, 3]),
                name="budget_plan_horizon_1_2_3",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.number} · {self.planning_year}"

    def save(self, *args, **kwargs):
        if self.organization_id is None:
            if self.budget_id and self.budget.organization_id:
                self.organization_id = self.budget.organization_id
            else:
                organization = Organization.objects.order_by("id").first()
                if organization is not None:
                    self.organization = organization
        super().save(*args, **kwargs)

    @property
    def monthly_total(self):
        return self.months.aggregate(total=models.Sum("amount"))["total"] or 0

    @property
    def is_monthly_total_valid(self) -> bool:
        return self.monthly_total == self.annual_amount


class BudgetLimitMonth(models.Model):
    plan = models.ForeignKey(
        BudgetLimitPlan,
        verbose_name="План / лимит",
        on_delete=models.CASCADE,
        related_name="months",
    )
    month = models.PositiveSmallIntegerField("Месяц")
    amount = models.DecimalField("Сумма", max_digits=16, decimal_places=2, default=0)

    class Meta:
        ordering = ["month"]
        verbose_name = "Строка помесячного плана"
        verbose_name_plural = "Строки помесячного плана"
        constraints = [
            models.UniqueConstraint(fields=["plan", "month"], name="uniq_budget_plan_month"),
            models.CheckConstraint(
                condition=models.Q(month__gte=1, month__lte=12),
                name="budget_plan_month_1_12",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.plan.number} · {self.month:02d}"


class BudgetLimitAdjustment(models.Model):
    number = models.CharField("Номер документа", max_length=32, unique=True)
    document_date = models.DateField("Дата документа", default=timezone.localdate)
    base_plan = models.ForeignKey(
        BudgetLimitPlan,
        verbose_name="Базовый документ",
        on_delete=models.PROTECT,
        related_name="adjustments",
    )
    article = models.ForeignKey(CashFlowArticle, verbose_name="Статья ДДС", on_delete=models.PROTECT)
    target_plan = models.ForeignKey(
        BudgetLimitPlan,
        verbose_name="Целевой лимит для межкомпанийного переноса",
        on_delete=models.PROTECT,
        related_name="incoming_adjustments",
        null=True,
        blank=True,
    )
    target_organization = models.ForeignKey(
        Organization,
        verbose_name="Компания-получатель",
        on_delete=models.PROTECT,
        related_name="incoming_limit_adjustments",
        null=True,
        blank=True,
    )
    new_annual_amount = models.DecimalField("Новая сумма", max_digits=16, decimal_places=2)
    reason = models.TextField("Причина корректировки")
    version = models.PositiveIntegerField("Версия", default=1)
    status = models.CharField(
        "Статус",
        max_length=32,
        choices=BudgetPlanStatus.choices,
        default=BudgetPlanStatus.DRAFT,
    )
    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Утверждающий",
        on_delete=models.PROTECT,
        related_name="budget_adjustments_to_approve",
    )
    approved_at = models.DateTimeField("Дата утверждения", null=True, blank=True)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Автор",
        on_delete=models.PROTECT,
        related_name="budget_adjustments",
    )
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        ordering = ["-document_date", "-created_at"]
        verbose_name = "Корректировка лимита"
        verbose_name_plural = "Корректировки лимитов"

    def __str__(self) -> str:
        return f"{self.number} · {self.base_plan.number}"

    @property
    def monthly_total(self):
        return self.months.aggregate(total=models.Sum("amount"))["total"] or 0

    @property
    def is_monthly_total_valid(self) -> bool:
        return self.monthly_total == self.new_annual_amount


class BudgetLimitAdjustmentMonth(models.Model):
    adjustment = models.ForeignKey(
        BudgetLimitAdjustment,
        verbose_name="Корректировка лимита",
        on_delete=models.CASCADE,
        related_name="months",
    )
    month = models.PositiveSmallIntegerField("Месяц")
    amount = models.DecimalField("Сумма", max_digits=16, decimal_places=2, default=0)

    class Meta:
        ordering = ["month"]
        verbose_name = "Строка помесячной корректировки"
        verbose_name_plural = "Строки помесячной корректировки"
        constraints = [
            models.UniqueConstraint(fields=["adjustment", "month"], name="uniq_budget_adjustment_month"),
            models.CheckConstraint(
                condition=models.Q(month__gte=1, month__lte=12),
                name="budget_adjustment_month_1_12",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.adjustment.number} · {self.month:02d}"

