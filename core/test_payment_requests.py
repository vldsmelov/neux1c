"""Заявки на оплату: жизненный цикл, валидация, prefill, загрузка файла, AV-хук."""

from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from core.models import (
    AuditAction,
    AuditLog,
    Currency,
    Organization,
    PaymentLimitControlMode,
    PaymentRequest,
    PaymentRequestControlSettings,
    PaymentRequestKind,
    PaymentRequestStatus,
)
from core.services.payment_requests import create_payment_request, submit_payment_request
from core.test_payments_base import PaymentRequestTestBase


def _fake_av_reject(uploaded_file):
    """Stub used via override_settings to simulate an AV scanner finding malware."""
    from django.core.exceptions import ValidationError

    raise ValidationError("AV-сканер обнаружил угрозу")


class PaymentRequestWorkflowTests(PaymentRequestTestBase):
    def test_payment_request_ui_workflow(self):

        self.client.login(username="economist", password="demo12345")
        create_response = self.client.post(
            reverse("payment_requests"),
            {
                "action": "create",
                "request_kind": PaymentRequestKind.BY_CONTRACT,
                "organization_id": Organization.objects.first().id,
                "article_id": self.article.id,
                "counterparty_id": self.contract.counterparty_id,
                "contract_id": self.contract.id,
                "invoice_number": "",
                "currency_id": Currency.objects.get(code="RUB").id,
                "amount": "100000.00",
                "manual_exchange_rate": "1.0000",
                "approver_id": self.manager.id,
                "payment_purpose": "Оплата поставки",
                "comment": "Pilot request",
            },
        )
        payment_request = PaymentRequest.objects.get()
        submit_response = self.client.post(
            reverse("payment_requests"),
            {"action": "submit", "request_id": payment_request.id},
        )

        self.assertRedirects(create_response, reverse("payment_requests"))
        self.assertRedirects(submit_response, reverse("payment_requests"))
        payment_request.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequestStatus.PENDING_APPROVAL)

        self.client.logout()
        self.client.login(username="manager", password="demo12345")
        approve_response = self.client.post(
            reverse("payment_requests"),
            {"action": "approve", "request_id": payment_request.id},
        )
        self.assertRedirects(approve_response, reverse("payment_requests"))
        payment_request.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequestStatus.APPROVED)

        self.client.logout()
        self.client.login(username="economist", password="demo12345")
        transfer_response = self.client.post(
            reverse("payment_requests"),
            {"action": "transfer", "request_id": payment_request.id},
        )
        self.assertRedirects(transfer_response, reverse("payment_requests"))
        payment_request.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequestStatus.TRANSFERRED)
        self.assertTrue(payment_request.do_external_id.startswith("DO-"))

    def test_contract_fields_are_autofilled_on_create(self):

        self.client.login(username="economist", password="demo12345")
        response = self.client.post(
            reverse("payment_requests"),
            {
                "action": "create",
                "request_kind": PaymentRequestKind.BY_CONTRACT,
                "organization_id": Organization.objects.first().id,
                "article_id": self.article.id,
                "counterparty_id": "",
                "contract_id": self.contract.id,
                "invoice_number": "",
                "currency_id": "",
                "amount": "",
                "manual_exchange_rate": "",
                "approver_id": self.manager.id,
                "payment_purpose": "Autofill purpose",
                "comment": "Autofill from contract",
            },
        )

        self.assertRedirects(response, reverse("payment_requests"))
        payment_request = PaymentRequest.objects.get()
        self.assertEqual(payment_request.counterparty_id, self.contract.counterparty_id)
        self.assertEqual(payment_request.currency_id, self.contract.currency_id)
        self.assertEqual(payment_request.amount, self.contract.amount)
        self.assertEqual(payment_request.manual_exchange_rate, self.contract.manual_exchange_rate)

    def test_block_mode_rejects_exceeded_request(self):

        PaymentRequestControlSettings.objects.create(
            name="Block",
            control_mode=PaymentLimitControlMode.BLOCK,
            is_active=True,
        )

        with self.assertRaises(ValueError):
            create_payment_request(
                author=self.economist,
                request_kind=PaymentRequestKind.BY_CONTRACT,
                organization=Organization.objects.first(),
                article=self.article,
                counterparty=self.contract.counterparty,
                contract=self.contract,
                currency=Currency.objects.get(code="RUB"),
                amount=Decimal("5000000.00"),
                manual_exchange_rate=Decimal("1.0000"),
                approver=self.manager,
                payment_purpose="Оплата поставки",
            )

    def test_warning_mode_allows_exceeded_request(self):

        PaymentRequestControlSettings.objects.create(
            name="Warn",
            control_mode=PaymentLimitControlMode.WARNING,
            is_active=True,
        )

        payment_request = create_payment_request(
            author=self.economist,
            request_kind=PaymentRequestKind.BY_CONTRACT,
            organization=Organization.objects.first(),
            article=self.article,
            counterparty=self.contract.counterparty,
            contract=self.contract,
            currency=Currency.objects.get(code="RUB"),
            amount=Decimal("5000000.00"),
            manual_exchange_rate=Decimal("1.0000"),
            approver=self.manager,
            payment_purpose="Оплата поставки",
        )
        self.assertTrue(payment_request.limit_exceeded)
        self.assertLess(payment_request.limit_remaining_after_rub, 0)
        submit_payment_request(payment_request, self.economist)
        payment_request.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequestStatus.PENDING_APPROVAL)

    def test_invoice_request_requires_invoice_number(self):

        with self.assertRaises(ValueError):
            create_payment_request(
                author=self.economist,
                request_kind=PaymentRequestKind.BY_INVOICE,
                organization=Organization.objects.first(),
                article=self.article,
                counterparty=self.contract.counterparty,
                contract=None,
                currency=Currency.objects.get(code="RUB"),
                amount=Decimal("150000.00"),
                manual_exchange_rate=Decimal("1.0000"),
                approver=self.manager,
                payment_purpose="Оплата по счету",
                invoice_number="",
            )

    def test_invoice_request_requires_invoice_date(self):

        with self.assertRaises(ValueError):
            create_payment_request(
                author=self.economist,
                request_kind=PaymentRequestKind.BY_INVOICE,
                organization=Organization.objects.first(),
                article=self.article,
                counterparty=self.contract.counterparty,
                contract=None,
                currency=Currency.objects.get(code="RUB"),
                amount=Decimal("150000.00"),
                manual_exchange_rate=Decimal("1.0000"),
                approver=self.manager,
                payment_purpose="Оплата по счету",
                invoice_number="INV-2026-001",
                invoice_date=None,
            )

    def test_without_contract_requires_justification(self):

        with self.assertRaises(ValueError):
            create_payment_request(
                author=self.economist,
                request_kind=PaymentRequestKind.WITHOUT_CONTRACT,
                organization=Organization.objects.first(),
                article=self.article,
                counterparty=self.contract.counterparty,
                contract=None,
                currency=Currency.objects.get(code="RUB"),
                amount=Decimal("35000.00"),
                manual_exchange_rate=Decimal("1.0000"),
                approver=self.manager,
                payment_purpose="Прочие расходы",
                justification_text="",
            )

    def test_justification_file_extension_is_validated(self):

        payload = SimpleUploadedFile(
            "script.exe",
            b"not a document",
            content_type="application/octet-stream",
        )

        with self.assertRaisesMessage(ValueError, "Недопустимый тип файла обоснования"):
            create_payment_request(
                author=self.economist,
                request_kind=PaymentRequestKind.BY_CONTRACT,
                organization=Organization.objects.first(),
                article=self.article,
                counterparty=self.contract.counterparty,
                contract=self.contract,
                currency=Currency.objects.get(code="RUB"),
                amount=Decimal("100000.00"),
                manual_exchange_rate=Decimal("1.0000"),
                approver=self.manager,
                payment_purpose="Оплата поставки",
                justification_file=payload,
            )


    def test_manager_can_reject_with_comment(self):

        payment_request = create_payment_request(
            author=self.economist,
            request_kind=PaymentRequestKind.BY_CONTRACT,
            organization=Organization.objects.first(),
            article=self.article,
            counterparty=self.contract.counterparty,
            contract=self.contract,
            currency=Currency.objects.get(code="RUB"),
            amount=Decimal("100000.00"),
            manual_exchange_rate=Decimal("1.0000"),
            approver=self.manager,
            payment_purpose="Оплата поставки",
            comment="На проверку",
        )
        submit_payment_request(payment_request, self.economist)

        self.client.login(username="manager", password="demo12345")
        response = self.client.post(
            reverse("payment_requests"),
            {
                "action": "reject",
                "request_id": payment_request.id,
                "approver_comment": "Нужно уточнить назначение платежа",
            },
        )

        self.assertRedirects(response, reverse("payment_requests"))
        payment_request.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequestStatus.REJECTED)
        self.assertEqual(payment_request.approver_comment, "Нужно уточнить назначение платежа")
        self.assertIsNotNone(payment_request.rejected_at)
        self.assertTrue(
            AuditLog.objects.filter(
                object_type="PaymentRequest",
                object_id=str(payment_request.id),
                action=AuditAction.UPDATE,
            ).exists()
        )

    def test_payment_request_wizard_prefills_on_validation_error(self):

        self.client.login(username="economist", password="demo12345")
        response = self.client.post(
            reverse("payment_request_wizard"),
            {
                "request_kind": PaymentRequestKind.BY_INVOICE,
                "organization_id": Organization.objects.first().id,
                "article_id": self.article.id,
                "counterparty_id": self.contract.counterparty_id,
                "currency_id": Currency.objects.get(code="RUB").id,
                "amount": "777777.77",
                "manual_exchange_rate": "1.0000",
                "approver_id": self.manager.id,
                "payment_purpose": "Тестовое назначение",
                "comment": "Должно остаться в форме",
                # invoice_number / invoice_date намеренно пропущены — это валидационная ошибка для BY_INVOICE
            },
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("777777.77", content)
        self.assertIn("Тестовое назначение", content)
        self.assertIn("Должно остаться в форме", content)
        # Сообщение об ошибке должно быть в messages
        msgs = [m.message for m in response.context["messages"]]
        self.assertTrue(any(msgs), "Ожидаем сообщение об ошибке валидации")

    def test_justification_download_is_access_controlled(self):
        """Author, approver и administrator получают файл; посторонний → 403; чужой/без файла → 404."""

        # Создаём заявку с приложенным PDF-файлом
        payload = SimpleUploadedFile("evidence.pdf", b"%PDF-1.4 fake", content_type="application/pdf")
        payment_request = create_payment_request(
            author=self.economist,
            request_kind=PaymentRequestKind.BY_CONTRACT,
            organization=Organization.objects.first(),
            article=self.article,
            counterparty=self.contract.counterparty,
            contract=self.contract,
            currency=Currency.objects.get(code="RUB"),
            amount=Decimal("100000.00"),
            manual_exchange_rate=Decimal("1.0000"),
            approver=self.manager,
            payment_purpose="Оплата поставки",
            justification_file=payload,
        )
        url = reverse("payment_request_justification_download", args=[payment_request.id])

        # Автор (economist) — 200 + Content-Disposition: attachment
        self.client.login(username="economist", password="demo12345")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn(payment_request.number, response["Content-Disposition"])
        # Аудит-лог должен зафиксировать скачивание
        self.assertTrue(
            AuditLog.objects.filter(
                object_type="PaymentRequest.justification_file",
                object_id=str(payment_request.id),
                action=AuditAction.VIEW,
            ).exists()
        )

        # Согласующий (manager) — 200
        self.client.logout()
        self.client.login(username="manager", password="demo12345")
        self.assertEqual(self.client.get(url).status_code, 200)

        # Администратор — 200 (через is_administrator-проверку)
        self.client.logout()
        self.client.login(username="admin", password="demo12345")
        self.assertEqual(self.client.get(url).status_code, 200)

        # Посторонний пользователь (бухгалтер) — 403
        self.client.logout()
        self.client.login(username="accountant", password="demo12345")
        self.assertEqual(self.client.get(url).status_code, 403)

        # Несуществующая заявка → 404
        self.client.logout()
        self.client.login(username="economist", password="demo12345")
        self.assertEqual(self.client.get(reverse("payment_request_justification_download", args=[99999])).status_code, 404)

    def test_av_scanner_hook_can_reject_upload(self):
        """Если задан NE_UX_AV_SCANNER, он вызывается и его ValidationError блокирует загрузку."""

        with override_settings(NE_UX_AV_SCANNER="core.test_payment_requests._fake_av_reject"):
            payload = SimpleUploadedFile("evidence.pdf", b"INFECTED", content_type="application/pdf")
            with self.assertRaisesMessage(ValueError, "AV-сканер"):
                from core.models import Currency, Organization

                create_payment_request(
                    author=self.economist,
                    request_kind=PaymentRequestKind.BY_CONTRACT,
                    organization=Organization.objects.first(),
                    article=self.article,
                    counterparty=self.contract.counterparty,
                    contract=self.contract,
                    currency=Currency.objects.get(code="RUB"),
                    amount=Decimal("100000.00"),
                    manual_exchange_rate=Decimal("1.0000"),
                    approver=self.manager,
                    payment_purpose="Оплата поставки",
                    justification_file=payload,
                )

