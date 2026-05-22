from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from core.integrations.one_c.mock import MockOneCProvider
from core.models import PaymentFact, PaymentFactAdjustment, PaymentFactControlSettings
from core.services.one_c_sync import sync_one_c_dataset
from core.services.payment_fact_adjustments import adjust_payment_fact


class PaymentFactAdjustmentsTests(TestCase):
    def setUp(self):
        call_command("setup_access_roles", "--with-users", verbosity=0)
        sync_one_c_dataset(MockOneCProvider())
        User = get_user_model()
        self.economist = User.objects.get(username="economist")
        self.manager = User.objects.get(username="manager")
        self.fact = PaymentFact.objects.get(external_id="pay-0001", accounting_kind="bu")

    def test_service_adjustment_creates_version_and_updates_fact(self):
        adjustment = adjust_payment_fact(
            payment_fact=self.fact,
            author=self.economist,
            new_amount=Decimal("900000.00"),
            reason="Уточнение суммы по выписке",
            new_comment="Корректировка экономистом",
        )
        self.fact.refresh_from_db()

        self.assertEqual(adjustment.version, 1)
        self.assertEqual(adjustment.previous_amount, Decimal("800000.00"))
        self.assertEqual(self.fact.amount, Decimal("900000.00"))
        self.assertEqual(self.fact.comment, "Корректировка экономистом")

        adjust_payment_fact(
            payment_fact=self.fact,
            author=self.economist,
            new_amount=Decimal("910000.00"),
            reason="Уточнение НДС",
            new_comment="Вторая версия",
        )
        self.assertEqual(PaymentFactAdjustment.objects.filter(payment_fact=self.fact).count(), 2)
        self.assertEqual(PaymentFactAdjustment.objects.filter(payment_fact=self.fact).first().version, 2)

    def test_closed_period_blocks_adjustment(self):
        PaymentFactControlSettings.objects.create(
            name="Закрытый период",
            is_active=True,
            closed_through=date(2026, 3, 31),
        )

        with self.assertRaises(ValueError):
            adjust_payment_fact(
                payment_fact=self.fact,
                author=self.economist,
                new_amount=Decimal("900000.00"),
                reason="Недопустимо",
                new_comment="",
            )

        self.fact.refresh_from_db()
        self.assertEqual(self.fact.amount, Decimal("800000.00"))
        self.assertFalse(PaymentFactAdjustment.objects.filter(payment_fact=self.fact).exists())

    def test_economist_can_adjust_fact_in_view(self):
        self.client.login(username="economist", password="demo12345")

        response = self.client.post(
            reverse("payment_facts"),
            {
                "action": "adjust",
                "fact_id": self.fact.id,
                "new_amount": "920000.00",
                "new_comment": "Через UI",
                "reason": "Сверка с реестром",
            },
        )

        self.assertRedirects(response, reverse("payment_facts"))
        self.fact.refresh_from_db()
        self.assertEqual(self.fact.amount, Decimal("920000.00"))
        self.assertEqual(PaymentFactAdjustment.objects.filter(payment_fact=self.fact).count(), 1)

    def test_manager_cannot_adjust_fact_in_view(self):
        self.client.login(username="manager", password="demo12345")

        response = self.client.post(
            reverse("payment_facts"),
            {
                "action": "adjust",
                "fact_id": self.fact.id,
                "new_amount": "920000.00",
                "new_comment": "Через UI",
                "reason": "Попытка руководителя",
            },
        )

        self.assertRedirects(response, reverse("payment_facts"))
        self.fact.refresh_from_db()
        self.assertEqual(self.fact.amount, Decimal("800000.00"))
        self.assertFalse(PaymentFactAdjustment.objects.filter(payment_fact=self.fact).exists())

    def test_payment_fact_history_page_shows_base_and_each_adjustment(self):
        from core.services.payment_fact_adjustments import adjust_payment_fact

        # Делаем две корректировки подряд
        adjust_payment_fact(
            payment_fact=self.fact,
            author=self.economist,
            new_amount=Decimal("850000.00"),
            reason="Уточнение БУ",
            new_comment="Сверка по акту",
        )
        adjust_payment_fact(
            payment_fact=self.fact,
            author=self.economist,
            new_amount=Decimal("900000.00"),
            reason="Доплата",
            new_comment="Доп.акт",
        )

        self.client.login(username="manager", password="demo12345")
        response = self.client.get(reverse("payment_fact_history", args=[self.fact.id]))
        self.assertEqual(response.status_code, 200)
        # На странице обе версии и причины присутствуют
        self.assertEqual(len(response.context["adjustments"]), 2)
        self.assertContains(response, "Уточнение БУ")
        self.assertContains(response, "Доплата")
        # latest_version отражает реальную версию
        self.assertEqual(response.context["latest_version"], 2)
        # base — это previous_amount первой корректировки (800000)
        self.assertEqual(response.context["base_amount"], Decimal("800000.00"))
