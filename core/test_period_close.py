"""Тесты Period Close — закрытия учётных периодов."""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from core.models import (
    Contract,
    ContractKind,
    Counterparty,
    Currency,
    FundingCase,
    Organization,
)
from core.services.period_close import (
    PeriodLockedError,
    assert_period_open,
    close_period,
    is_period_locked,
    reopen_period,
)


class PeriodCloseTests(TestCase):
    def setUp(self):
        call_command("setup_access_roles", "--with-users", verbosity=0)
        User = get_user_model()
        self.admin = User.objects.get(username="admin")
        self.economist = User.objects.get(username="economist")
        self.org = Organization.objects.create(name="Тест Орг PC", source_system="manual")

    def test_period_can_be_closed_and_reopened(self):
        lock = close_period(
            organization=self.org, year=2026, month=3, user=self.admin,
            comment="Закрытие после сверки с банком",
        )
        self.assertTrue(is_period_locked(self.org, date(2026, 3, 15)))
        self.assertFalse(is_period_locked(self.org, date(2026, 4, 5)))

        reopen_period(lock, self.economist, "Корректировка по аудиту")
        self.assertFalse(is_period_locked(self.org, date(2026, 3, 15)))
        lock.refresh_from_db()
        self.assertIsNotNone(lock.unlocked_at)
        self.assertEqual(lock.unlock_reason, "Корректировка по аудиту")

    def test_assert_period_open_raises_when_locked(self):
        close_period(organization=self.org, year=2026, month=3, user=self.admin)
        with self.assertRaises(PeriodLockedError):
            assert_period_open(self.org, date(2026, 3, 10))

    def test_reopen_requires_reason(self):
        lock = close_period(organization=self.org, year=2026, month=3, user=self.admin)
        with self.assertRaises(ValueError):
            reopen_period(lock, self.economist, "")

    def test_loan_repayment_blocked_in_closed_period(self):
        """Quick-action возврата займа должен падать с понятной ошибкой в закрытом периоде."""
        rub = Currency.objects.create(code="RUB", name="Рубль", source_system="manual")
        lender = Counterparty.objects.create(name="Кредитор PC", source_system="manual")
        case = FundingCase.objects.create(
            code="PC-CASE", name="Period close test", organization=self.org,
        )
        loan = Contract.objects.create(
            number="PC-LOAN", date=date(2026, 1, 1), kind=ContractKind.LOAN_RECEIVED,
            name="Заём", counterparty=lender, currency=rub,
            amount=Decimal("100000.00"), funding_case=case,
        )
        # Закрываем март
        close_period(organization=self.org, year=2026, month=3, user=self.admin)
        # Создаём статью для возврата
        from core.models import CashFlowArticle
        article = CashFlowArticle.objects.create(
            code="PC-DDS-OUT", name="Возврат PC", direction="outflow",
        )

        self.client.login(username="economist", password="demo12345")
        response = self.client.post(
            reverse("funding_case_detail", args=[case.id]),
            {
                "action": "record_loan_repayment",
                "loan_contract_id": loan.id,
                "article_id": article.id,
                "date": "2026-03-15",  # внутри закрытого периода
                "amount": "30000.00",
                "interest_portion": "0.00",
            },
            follow=True,
        )
        msgs = [m.message for m in response.context["messages"]]
        self.assertTrue(any("закрыт" in m.lower() for m in msgs), msgs)
        # Факт не создан
        from core.models import PaymentFact
        self.assertFalse(PaymentFact.objects.filter(loan_repayment_contract=loan).exists())

    def test_period_close_view_lists_summary(self):
        close_period(organization=self.org, year=2026, month=3, user=self.admin)
        self.client.login(username="admin", password="demo12345")
        response = self.client.get(reverse("period_close"), {"organization_id": self.org.id})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.org.name)
