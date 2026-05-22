from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from core.integrations.one_c.mock import MockOneCProvider
from core.models import (
    AuditAction,
    AuditLog,
    CashFlowArticle,
    Contract,
    PaymentLimitControlMode,
    PaymentRequest,
    PaymentRequestControlSettings,
    PaymentRequestKind,
    PaymentRequestStatus,
)
from core.services.budget_planning import approve_budget_plan, create_budget_plan, submit_budget_plan
from core.services.one_c_sync import sync_one_c_dataset
from core.services.payment_requests import create_payment_request, submit_payment_request


def _fake_av_reject(uploaded_file):
    """Test stub used by override_settings to simulate an AV-scanner finding malware."""
    from django.core.exceptions import ValidationError

    raise ValidationError("AV-сканер обнаружил угрозу")


class PaymentRequestWorkflowTests(TestCase):
    def setUp(self):
        call_command("setup_access_roles", "--with-users", verbosity=0)
        sync_one_c_dataset(MockOneCProvider())
        User = get_user_model()
        self.economist = User.objects.get(username="economist")
        self.manager = User.objects.get(username="manager")
        self.contract = Contract.objects.filter(kind="sole_supplier").order_by("id").first()
        self.assertIsNotNone(self.contract)
        self.article = CashFlowArticle.objects.get(code="DDS-010")
        self._approve_limit_for_article(self.article, Decimal("2000000.00"))

    def test_payment_request_ui_workflow(self):
        from core.models import Currency, Organization

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
        from core.models import Organization

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
        from core.models import Currency, Organization

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
        from core.models import Currency, Organization

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
        from core.models import Currency, Organization

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
        from core.models import Currency, Organization

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
        from core.models import Currency, Organization

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
        from core.models import Currency, Organization

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

    def test_payment_workflow_fires_notifications_to_correct_recipients(self):
        from core.models import Currency, Notification, NotificationKind, Organization
        from core.services.payment_requests import (
            approve_payment_request,
            reject_payment_request,
            transfer_payment_request_to_do,
        )

        # submit → notify approver
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
            payment_purpose="Оплата",
        )
        submit_payment_request(payment_request, self.economist)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.manager,
                kind=NotificationKind.PAYMENT_SUBMITTED,
                payload_object_id=str(payment_request.id),
            ).exists()
        )

        # reject → notify author с комментарием в text
        reject_payment_request(payment_request, self.manager, "Уточните основание")
        rejection = Notification.objects.get(
            recipient=self.economist,
            kind=NotificationKind.PAYMENT_REJECTED,
            payload_object_id=str(payment_request.id),
        )
        self.assertIn("Уточните основание", rejection.text)

        # Снова отправляем после исправления → approve → notify author
        payment_request.status = "draft"
        payment_request.save(update_fields=["status"])
        submit_payment_request(payment_request, self.economist)
        approve_payment_request(payment_request, self.manager)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.economist,
                kind=NotificationKind.PAYMENT_APPROVED,
                payload_object_id=str(payment_request.id),
            ).exists()
        )

        # transfer → notify author
        transfer_payment_request_to_do(payment_request, self.economist)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.economist,
                kind=NotificationKind.PAYMENT_TRANSFERRED,
                payload_object_id=str(payment_request.id),
            ).exists()
        )

    def test_limit_overrun_notification_on_submit(self):
        """При warning-mode превышение лимита должно дать LIMIT_OVERRUN автору и согласующему."""
        from core.models import (
            Currency,
            Notification,
            NotificationKind,
            Organization,
            PaymentLimitControlMode,
            PaymentRequestControlSettings,
        )

        # warning-mode (не block) — лимит превышаем, но заявка проходит
        PaymentRequestControlSettings.objects.all().delete()
        PaymentRequestControlSettings.objects.create(
            control_mode=PaymentLimitControlMode.WARNING, is_active=True,
        )
        # Делаем сумму заведомо выше approved-лимита (в setUp лимит 2_000_000)
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
            payment_purpose="Превышение",
        )
        submit_payment_request(payment_request, self.economist)
        # Уведомление автору и руководителю
        overruns = Notification.objects.filter(
            kind=NotificationKind.LIMIT_OVERRUN,
            payload_object_id=str(payment_request.id),
        )
        recipients = set(overruns.values_list("recipient_id", flat=True))
        self.assertIn(self.economist.id, recipients)
        self.assertIn(self.manager.id, recipients)

    def test_notifications_page_and_mark_read(self):
        from core.models import Notification, NotificationKind
        from core.services.notifications import notify

        # setUp создал утверждённый лимит → LIMIT_APPROVED уведомление автору
        # уже есть. Считаем baseline до нашего теста.
        baseline_unread = Notification.objects.filter(
            recipient=self.economist, read_at__isnull=True
        ).count()
        notify(
            recipient=self.economist,
            kind=NotificationKind.PAYMENT_APPROVED,
            title="Тестовое",
            text="Содержание",
            link="/payments/requests/",
        )
        self.client.login(username="economist", password="demo12345")

        # Страница доступна, показывает уведомление
        response = self.client.get(reverse("notifications"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["unread_total"], baseline_unread + 1)
        self.assertContains(response, "Тестовое")

        # Пометка всех как прочитанных
        response = self.client.post(reverse("notifications"), {"action": "mark_all_read"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            Notification.objects.filter(recipient=self.economist, read_at__isnull=True).count(),
            0,
        )

        # Фильтр непрочитанных пуст
        response = self.client.get(reverse("notifications"), {"filter": "unread"})
        self.assertEqual(response.context["page"].paginator.count, 0)

    def test_manager_can_reject_with_comment(self):
        from core.models import Currency, Organization

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
        from core.models import Currency, Organization

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
        from django.contrib.auth.models import Group
        from core.models import Currency, Organization

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
        from django.test import override_settings

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

    def _approve_limit_for_article(self, article, amount: Decimal):
        from core.models import Currency, Department

        monthly_amounts = {month: (amount / Decimal("12")).quantize(Decimal("0.01")) for month in range(1, 12)}
        monthly_amounts[12] = amount - sum(monthly_amounts.values())
        plan = create_budget_plan(
            author=self.economist,
            department=Department.objects.first(),
            article=article,
            currency=Currency.objects.get(code="RUB"),
            planning_year=2026,
            planning_horizon=1,
            annual_amount=amount,
            approver=self.manager,
            monthly_amounts=monthly_amounts,
            comment="Лимит для заявок",
        )
        submit_budget_plan(plan, self.economist)
        approve_budget_plan(plan, self.manager)
