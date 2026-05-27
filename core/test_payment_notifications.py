"""Заявки на оплату: уведомления, каскад лимитов, SLA-просрочки."""

from decimal import Decimal

from django.urls import reverse

from core.models import (
    Currency,
    Notification,
    NotificationKind,
    Organization,
    PaymentLimitControlMode,
    PaymentRequestControlSettings,
    PaymentRequestKind,
)
from core.services.payment_requests import create_payment_request, submit_payment_request
from core.test_payments_base import PaymentRequestTestBase


class PaymentNotificationTests(PaymentRequestTestBase):
    def test_payment_workflow_fires_notifications_to_correct_recipients(self):
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


    def test_approving_limit_adjustment_cascades_to_pending_requests(self):
        """Утверждение корректировки, уменьшающей лимит, должно перевести
        ранее ‘в норме’ pending-заявку в превышение и уведомить участников."""
        from core.models import (
            BudgetLimitPlan,
        )
        from core.services.budget_planning import (
            approve_limit_adjustment,
            create_limit_adjustment,
            submit_limit_adjustment,
        )

        # В setUp создан approved-лимит на 2_000_000 для self.article (DDS-010).
        # Mock-1C добавил BU-факт 800k по той же статье, поэтому доступный
        # остаток на старте = 1_200_000. Заявка на 1_000_000 «в норме».
        payment_request = create_payment_request(
            author=self.economist,
            request_kind=PaymentRequestKind.BY_CONTRACT,
            organization=Organization.objects.first(),
            article=self.article,
            counterparty=self.contract.counterparty,
            contract=self.contract,
            currency=Currency.objects.get(code="RUB"),
            amount=Decimal("1000000.00"),
            manual_exchange_rate=Decimal("1.0000"),
            approver=self.manager,
            payment_purpose="В норме",
        )
        submit_payment_request(payment_request, self.economist)
        payment_request.refresh_from_db()
        self.assertFalse(payment_request.limit_exceeded)

        # Создаём и утверждаем корректировку: годовая сумма падает до 900_000.
        # После корректировки: лимит 900k - факт 800k = 100k свободно, заявка
        # 1_000_000 → превышение на 900_000.
        plan = BudgetLimitPlan.objects.filter(article=self.article).first()
        monthly_amounts = {m: Decimal("900000.00") / 12 for m in range(1, 13)}
        monthly_amounts[12] = Decimal("900000.00") - sum(monthly_amounts[m] for m in range(1, 12))
        adjustment = create_limit_adjustment(
            author=self.economist,
            base_plan=plan,
            new_annual_amount=Decimal("900000.00"),
            reason="Сокращение",
            monthly_amounts=monthly_amounts,
        )
        submit_limit_adjustment(adjustment, self.economist)
        approve_limit_adjustment(adjustment, self.manager)

        # Каскад должен пересчитать pending-заявку: теперь она превышает лимит
        payment_request.refresh_from_db()
        self.assertTrue(payment_request.limit_exceeded)
        # И оба участника получили LIMIT_OVERRUN
        overruns = Notification.objects.filter(
            kind=NotificationKind.LIMIT_OVERRUN,
            payload_object_id=str(payment_request.id),
        )
        recipients = set(overruns.values_list("recipient_id", flat=True))
        self.assertIn(self.economist.id, recipients)
        self.assertIn(self.manager.id, recipients)

    def test_sla_digest_command_notifies_approver_once_per_queue(self):
        """send_pending_sla_digest шлёт согласующему один дайджест на всю очередь."""
        from datetime import timedelta
        from django.core.management import call_command
        from django.utils import timezone

        org = Organization.objects.first()
        # Две просроченные pending-заявки одного согласующего
        for i in range(2):
            pr = create_payment_request(
                author=self.economist,
                request_kind=PaymentRequestKind.BY_CONTRACT,
                organization=org,
                article=self.article,
                counterparty=self.contract.counterparty,
                contract=self.contract,
                currency=Currency.objects.get(code="RUB"),
                amount=Decimal("100000.00"),
                manual_exchange_rate=Decimal("1.0000"),
                approver=self.manager,
                payment_purpose=f"SLA {i}",
            )
            submit_payment_request(pr, self.economist)
            pr.submitted_at = timezone.now() - timedelta(days=5)
            pr.save(update_fields=["submitted_at"])

        before = Notification.objects.filter(recipient=self.manager).count()
        call_command("send_pending_sla_digest", verbosity=0)
        after = Notification.objects.filter(recipient=self.manager).count()
        # Ровно одно новое уведомление-дайджест, несмотря на 2 просроченные заявки
        self.assertEqual(after - before, 1)
        digest = Notification.objects.filter(recipient=self.manager).order_by("-created_at").first()
        self.assertIn("Просрочено заявок", digest.title)

    def test_submit_sets_submitted_at_and_dashboard_flags_overdue(self):
        """submitted_at пишется при submit; дашборд считает SLA по этому полю."""
        from datetime import timedelta
        from core.services.manager_dashboard import build_manager_dashboard
        from django.utils import timezone

        org = Organization.objects.first()
        pr = create_payment_request(
            author=self.economist,
            request_kind=PaymentRequestKind.BY_CONTRACT,
            organization=org,
            article=self.article,
            counterparty=self.contract.counterparty,
            contract=self.contract,
            currency=Currency.objects.get(code="RUB"),
            amount=Decimal("100000.00"),
            manual_exchange_rate=Decimal("1.0000"),
            approver=self.manager,
            payment_purpose="SLA",
        )
        submit_payment_request(pr, self.economist)
        pr.refresh_from_db()
        self.assertIsNotNone(pr.submitted_at)

        # Свежеотправленная заявка — не просрочена
        payload = build_manager_dashboard(year=pr.request_date.year, organization_id=org.id)
        self.assertEqual(payload["overdue_pending_count"], 0)

        # Сдвигаем submitted_at на 5 дней назад → просрочена
        pr.submitted_at = timezone.now() - timedelta(days=5)
        pr.save(update_fields=["submitted_at"])
        payload = build_manager_dashboard(year=pr.request_date.year, organization_id=org.id)
        self.assertEqual(payload["overdue_pending_count"], 1)
        self.assertEqual(payload["overdue_pending"][0].id, pr.id)

