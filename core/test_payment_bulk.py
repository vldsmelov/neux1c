"""Заявки на оплату: серверные фильтры, CSV-экспорт, массовые операции, отмена."""

from decimal import Decimal

from django.urls import reverse

from core.models import (
    Currency,
    Notification,
    NotificationKind,
    Organization,
    PaymentRequestKind,
    PaymentRequestStatus,
)
from core.services.payment_requests import create_payment_request, submit_payment_request
from core.test_payments_base import PaymentRequestTestBase


class PaymentBulkAndFilterTests(PaymentRequestTestBase):
    def test_facts_journal_csv_export(self):
        from core.models import Organization, PaymentFact

        org = Organization.objects.first()
        # Mock-1C seeded BU/NU facts; pick a year that has data.
        fact = PaymentFact.objects.filter(organization=org).order_by("-date").first()
        self.assertIsNotNone(fact)

        self.client.login(username="economist", password="demo12345")
        session = self.client.session
        session["working_organization_id"] = org.id
        session.save()

        response = self.client.get(reverse("payment_facts"), {"year": fact.date.year, "export": "csv"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment", response["Content-Disposition"])
        body = response.content.decode("utf-8-sig")
        self.assertIn("Дата;Учёт;Счёт", body.splitlines()[0])
        # CSV escapes embedded quotes by doubling them, so check on a quote-free fragment
        self.assertIn(str(fact.amount), body)

    def test_limits_journal_csv_export(self):
        from core.models import BudgetLimitPlan, Organization

        org = Organization.objects.first()
        plan = BudgetLimitPlan.objects.filter(organization=org).first()
        self.assertIsNotNone(plan)

        self.client.login(username="economist", password="demo12345")
        session = self.client.session
        session["working_organization_id"] = org.id
        session.save()

        response = self.client.get(reverse("planning_limits"), {"export": "csv"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        body = response.content.decode("utf-8-sig")
        self.assertIn("Номер;Бюджет;Компания", body.splitlines()[0])
        self.assertIn(plan.number, body)

    def test_journal_csv_export_respects_filters(self):

        org = Organization.objects.first()
        # Две заявки в разных статусах
        draft = create_payment_request(
            author=self.economist,
            request_kind=PaymentRequestKind.BY_CONTRACT,
            organization=org,
            article=self.article,
            counterparty=self.contract.counterparty,
            contract=self.contract,
            currency=Currency.objects.get(code="RUB"),
            amount=Decimal("111111.00"),
            manual_exchange_rate=Decimal("1.0000"),
            approver=self.manager,
            payment_purpose="Draft",
        )
        pending = create_payment_request(
            author=self.economist,
            request_kind=PaymentRequestKind.BY_CONTRACT,
            organization=org,
            article=self.article,
            counterparty=self.contract.counterparty,
            contract=self.contract,
            currency=Currency.objects.get(code="RUB"),
            amount=Decimal("222222.00"),
            manual_exchange_rate=Decimal("1.0000"),
            approver=self.manager,
            payment_purpose="Pending",
        )
        submit_payment_request(pending, self.economist)

        self.client.login(username="economist", password="demo12345")
        session = self.client.session
        session["working_organization_id"] = org.id
        session.save()
        url = reverse("payment_requests")

        # Без фильтра — обе попадают
        response = self.client.get(url, {"export": "csv"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment", response["Content-Disposition"])
        body = response.content.decode("utf-8-sig")
        self.assertIn(draft.number, body)
        self.assertIn(pending.number, body)
        # Заголовки на русском (UTF-8 BOM присутствует)
        self.assertIn("Номер;Дата заявки", body.splitlines()[0])

        # Фильтр по статусу обрезает выборку
        response = self.client.get(url, {"status": "draft", "export": "csv"})
        body = response.content.decode("utf-8-sig")
        self.assertIn(draft.number, body)
        self.assertNotIn(pending.number, body)

    def test_journal_server_side_filters(self):
        """status / only_overrun / q сужают выборку на стороне БД."""

        org = Organization.objects.first()
        # Заявка 1: DRAFT — не отправлена
        draft = create_payment_request(
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
            payment_purpose="Draft",
        )
        # Заявка 2: PENDING
        pending = create_payment_request(
            author=self.economist,
            request_kind=PaymentRequestKind.BY_CONTRACT,
            organization=org,
            article=self.article,
            counterparty=self.contract.counterparty,
            contract=self.contract,
            currency=Currency.objects.get(code="RUB"),
            amount=Decimal("200000.00"),
            manual_exchange_rate=Decimal("1.0000"),
            approver=self.manager,
            payment_purpose="Pending normal",
        )
        submit_payment_request(pending, self.economist)

        self.client.login(username="economist", password="demo12345")
        session = self.client.session
        session["working_organization_id"] = org.id
        session.save()

        url = reverse("payment_requests")

        # Без фильтров — обе заявки
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        ids = {pr.id for pr in response.context["requests"]}
        self.assertIn(draft.id, ids)
        self.assertIn(pending.id, ids)

        # status=draft → только draft
        response = self.client.get(url, {"status": "draft"})
        ids = {pr.id for pr in response.context["requests"]}
        self.assertEqual(ids, {draft.id})

        # q по номеру → только эта заявка
        response = self.client.get(url, {"q": pending.number})
        ids = {pr.id for pr in response.context["requests"]}
        self.assertEqual(ids, {pending.id})

        # q по контрагенту → обе попадают (один counterparty)
        response = self.client.get(url, {"q": self.contract.counterparty.name[:5]})
        ids = {pr.id for pr in response.context["requests"]}
        self.assertIn(pending.id, ids)
        self.assertIn(draft.id, ids)

    def test_bulk_approve_processes_eligible_requests_and_skips_others(self):
        """Manager bulk-approves PENDING_APPROVAL; DRAFT в той же выборке пропускается."""

        # 3 заявки разных статусов
        pending_pairs = []
        for _ in range(2):
            pr = create_payment_request(
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
                payment_purpose="Bulk",
            )
            submit_payment_request(pr, self.economist)
            pending_pairs.append(pr)
        draft_pr = create_payment_request(
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
            payment_purpose="Draft",
        )

        # Меняем working_organization у сессии manager на ту, где живут заявки
        self.client.login(username="manager", password="demo12345")
        session = self.client.session
        session["working_organization_id"] = Organization.objects.first().id
        session.save()

        response = self.client.post(
            reverse("payment_requests"),
            {
                "action": "bulk_approve",
                "request_id": [str(p.id) for p in pending_pairs] + [str(draft_pr.id)],
            },
        )
        self.assertEqual(response.status_code, 302)

        for pr in pending_pairs:
            pr.refresh_from_db()
            self.assertEqual(pr.status, PaymentRequestStatus.APPROVED)
        draft_pr.refresh_from_db()
        self.assertEqual(draft_pr.status, PaymentRequestStatus.DRAFT)
        # Каждое успешное согласование создаёт уведомление автору
        self.assertEqual(
            Notification.objects.filter(
                kind=NotificationKind.PAYMENT_APPROVED,
                payload_object_id__in=[str(p.id) for p in pending_pairs],
            ).count(),
            len(pending_pairs),
        )

    def test_bulk_reject_requires_comment_and_writes_it_to_each_request(self):

        pr = create_payment_request(
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
            payment_purpose="Bulk reject",
        )
        submit_payment_request(pr, self.economist)

        self.client.login(username="manager", password="demo12345")
        session = self.client.session
        session["working_organization_id"] = Organization.objects.first().id
        session.save()

        # Без комментария — ошибка
        response = self.client.post(
            reverse("payment_requests"),
            {"action": "bulk_reject", "request_id": [str(pr.id)]},
            follow=True,
        )
        msgs = [m.message for m in response.context["messages"]]
        self.assertTrue(any("комментар" in m.lower() for m in msgs))
        pr.refresh_from_db()
        self.assertEqual(pr.status, PaymentRequestStatus.PENDING_APPROVAL)

        # С комментарием — отклоняется и комментарий записан
        response = self.client.post(
            reverse("payment_requests"),
            {
                "action": "bulk_reject",
                "request_id": [str(pr.id)],
                "approver_comment": "Все плохо, переделать",
            },
        )
        self.assertEqual(response.status_code, 302)
        pr.refresh_from_db()
        self.assertEqual(pr.status, PaymentRequestStatus.REJECTED)
        self.assertEqual(pr.approver_comment, "Все плохо, переделать")


    def test_author_can_cancel_own_pending_request(self):
        """Автор отменяет свою заявку → CANCELLED, approver получает уведомление,
        соседние pending-заявки пересчитываются (резерв освобождён)."""
        from core.services.payment_requests import cancel_payment_request

        pr = create_payment_request(
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
            payment_purpose="К отмене",
        )
        submit_payment_request(pr, self.economist)
        cancel_payment_request(pr, self.economist, "Передумал")
        pr.refresh_from_db()
        self.assertEqual(pr.status, PaymentRequestStatus.CANCELLED)
        self.assertEqual(pr.cancellation_reason, "Передумал")
        self.assertIsNotNone(pr.cancelled_at)
        # Approver получил уведомление об отзыве (PAYMENT_REJECTED kind = «снято с очереди»)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.manager,
                payload_object_id=str(pr.id),
                kind=NotificationKind.PAYMENT_REJECTED,
            ).exists()
        )

    def test_other_user_cannot_cancel_someone_elses_request(self):
        from core.services.payment_requests import cancel_payment_request

        pr = create_payment_request(
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
            payment_purpose="Чужая",
        )
        with self.assertRaisesMessage(ValueError, "автор"):
            cancel_payment_request(pr, self.manager, "Манагер не может")

    def test_cannot_cancel_request_after_approval(self):
        from core.services.payment_requests import (
            approve_payment_request,
            cancel_payment_request,
        )

        pr = create_payment_request(
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
            payment_purpose="После согласования",
        )
        submit_payment_request(pr, self.economist)
        approve_payment_request(pr, self.manager)
        with self.assertRaisesMessage(ValueError, "черновик или заявку"):
            cancel_payment_request(pr, self.economist, "Поздно")

