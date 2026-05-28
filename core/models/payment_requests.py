"""Заявки на оплату и настройки контроля лимита заявок."""

from django.conf import settings
from django.db import models
from django.utils import timezone

from ..validators import payment_request_justification_upload_to, validate_justification_file
from .enums import PaymentLimitControlMode, PaymentRequestKind, PaymentRequestStatus
from .nsi import CashFlowArticle, Counterparty, Currency, Organization
from .contracts import AdditionalAgreement, Contract


class PaymentRequestControlSettings(models.Model):
    name = models.CharField("Название", max_length=120, default="Контроль лимитов заявок")
    is_active = models.BooleanField("Активна", default=True)
    control_mode = models.CharField(
        "Режим контроля лимита",
        max_length=16,
        choices=PaymentLimitControlMode.choices,
        default=PaymentLimitControlMode.WARNING,
    )
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        verbose_name = "Настройка контроля лимита заявок"
        verbose_name_plural = "Настройки контроля лимита заявок"

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
            PaymentRequestControlSettings.objects.exclude(pk=self.pk).update(is_active=False)


class PaymentRequest(models.Model):
    number = models.CharField("Номер", max_length=32, unique=True)
    request_kind = models.CharField("Тип заявки", max_length=32, choices=PaymentRequestKind.choices)
    request_date = models.DateField("Дата заявки", default=timezone.localdate)
    organization = models.ForeignKey(Organization, verbose_name="Организация", on_delete=models.PROTECT)
    article = models.ForeignKey(CashFlowArticle, verbose_name="Статья ДДС", on_delete=models.PROTECT)
    counterparty = models.ForeignKey(Counterparty, verbose_name="Контрагент", on_delete=models.PROTECT)
    contract = models.ForeignKey(
        Contract,
        verbose_name="Договор",
        on_delete=models.PROTECT,
        related_name="payment_requests",
        null=True,
        blank=True,
    )
    additional_agreement = models.ForeignKey(
        AdditionalAgreement,
        verbose_name="Допсоглашение",
        on_delete=models.PROTECT,
        related_name="payment_requests",
        null=True,
        blank=True,
    )
    invoice_date = models.DateField("Дата счета", null=True, blank=True)
    payment_purpose = models.CharField("Назначение платежа", max_length=255, blank=True)
    invoice_number = models.CharField("Счет", max_length=64, blank=True)
    currency = models.ForeignKey(Currency, verbose_name="Валюта", on_delete=models.PROTECT)
    amount = models.DecimalField("Сумма", max_digits=16, decimal_places=2)
    manual_exchange_rate = models.DecimalField("Курс к RUB", max_digits=12, decimal_places=4, default=1)
    amount_rub = models.DecimalField("Сумма в RUB", max_digits=16, decimal_places=2)
    limit_remaining_before_rub = models.DecimalField("Остаток лимита до заявки, RUB", max_digits=16, decimal_places=2)
    limit_remaining_after_rub = models.DecimalField("Остаток лимита после заявки, RUB", max_digits=16, decimal_places=2)
    limit_exceeded = models.BooleanField("Превышение лимита", default=False)
    status = models.CharField("Статус", max_length=32, choices=PaymentRequestStatus.choices, default=PaymentRequestStatus.DRAFT)
    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Согласующий",
        on_delete=models.PROTECT,
        related_name="payment_requests_to_approve",
    )
    approver_comment = models.TextField("Комментарий согласующего", blank=True)
    submitted_at = models.DateTimeField("Дата отправки на согласование", null=True, blank=True)
    rejected_at = models.DateTimeField("Дата отклонения", null=True, blank=True)
    approved_at = models.DateTimeField("Дата согласования", null=True, blank=True)
    transferred_at = models.DateTimeField("Дата передачи в 1С:ДО", null=True, blank=True)
    cancelled_at = models.DateTimeField("Дата отмены автором", null=True, blank=True)
    cancellation_reason = models.CharField("Причина отмены", max_length=255, blank=True)
    do_external_id = models.CharField("ID в 1С:ДО", max_length=64, blank=True)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Автор",
        on_delete=models.PROTECT,
        related_name="payment_requests",
    )
    justification_text = models.TextField("Обоснование оплаты", blank=True)
    justification_file = models.FileField(
        "Файл обоснования",
        upload_to=payment_request_justification_upload_to,
        validators=[validate_justification_file],
        blank=True,
    )
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        ordering = ["-request_date", "-created_at"]
        verbose_name = "Заявка на оплату"
        verbose_name_plural = "Заявки на оплату"

    def __str__(self) -> str:
        return f"{self.number} · {self.counterparty.name}"

    # SLA for pending approval: см. core.services.manager_dashboard.PENDING_SLA_DAYS
    PENDING_SLA_DAYS = 3

    @property
    def is_overdue(self) -> bool:
        """True если заявка PENDING_APPROVAL и висит дольше SLA."""
        if self.status not in (PaymentRequestStatus.PENDING_APPROVAL, PaymentRequestStatus.PENDING_FINAL_APPROVAL) or not self.submitted_at:
            return False
        from datetime import timedelta
        from django.utils import timezone as _tz
        return self.submitted_at < _tz.now() - timedelta(days=self.PENDING_SLA_DAYS)

