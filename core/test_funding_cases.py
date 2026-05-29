"""Тесты кейсов финансирования."""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from core.integrations.one_c.mock import MockOneCProvider
from core.models import (
    AccountingKind,
    CashFlowArticle,
    Contract,
    ContractKind,
    Counterparty,
    Currency,
    FundingCase,
    FundingCaseStatus,
    Organization,
    PaymentDirection,
    PaymentFact,
)
from core.services.funding_cases import build_case_overview
from core.services.one_c_sync import sync_one_c_dataset


class FundingCaseTests(TestCase):
    def setUp(self):
        call_command("setup_access_roles", "--with-users", verbosity=0)
        sync_one_c_dataset(MockOneCProvider())
        User = get_user_model()
        self.economist = User.objects.get(username="economist")
        self.organization = Organization.objects.get(name='ООО "Гладиолус"')
        self.mother = Counterparty.objects.create(name='АО "Материнская"', source_system="manual")
        self.supplier = Counterparty.objects.create(name='ООО "Поставщик стройматериалов"', source_system="manual")
        self.lender = Counterparty.objects.create(name='ООО "Кредитор"', source_system="manual")
        self.rub = Currency.objects.get(code="RUB")
        self.article_in = CashFlowArticle.objects.create(code="DDS-IN-CASE", name="Поступление по ТЗ", direction="inflow")
        self.article_out = CashFlowArticle.objects.create(code="DDS-OUT-CASE", name="Закупка стройматериалов", direction="outflow")
        self.article_loan_in = CashFlowArticle.objects.create(code="DDS-LOAN-IN", name="Поступление займа", direction="inflow")

    def test_case_overview_handles_user_scenario(self):
        """Воспроизводим именно тот кейс что описал пользователь:
        - доходный договор от мамы на 2 000 000
        - расход поставщику 2 500 000
        - заём 500 000
        - возврат от мамы 500 000 покрывает заём
        """
        case = FundingCase.objects.create(
            code="FUND-001",
            name="Закупка стройматериалов по ТЗ материнской",
            organization=self.organization,
            owner=self.economist,
        )
        mother_contract = Contract.objects.create(
            number="CUST-001", date=date(2026, 1, 10),
            name="Доходный договор от мамы", kind=ContractKind.CUSTOMER,
            counterparty=self.mother, currency=self.rub,
            amount=Decimal("2000000.00"), funding_case=case,
        )
        supplier_contract = Contract.objects.create(
            number="SUP-001", date=date(2026, 1, 15),
            name="Поставщик стройматериалов", kind=ContractKind.SOLE_SUPPLIER,
            counterparty=self.supplier, currency=self.rub,
            amount=Decimal("2500000.00"), funding_case=case,
        )
        loan_contract = Contract.objects.create(
            number="LOAN-001", date=date(2026, 1, 20),
            name="Заём на покрытие дефицита", kind=ContractKind.LOAN_RECEIVED,
            counterparty=self.lender, currency=self.rub,
            amount=Decimal("500000.00"), interest_rate=Decimal("12.000"),
            maturity_date=date(2026, 6, 30), funding_case=case,
        )
        # Сначала только доход 2M от мамы и заём 500k
        PaymentFact.objects.create(
            external_id="case-test-1",
            date=date(2026, 1, 12), organization=self.organization,
            article=self.article_in, counterparty=self.mother, contract=mother_contract,
            amount=Decimal("2000000.00"), currency=self.rub,
            accounting_kind=AccountingKind.BU, direction=PaymentDirection.INFLOW,
            account="51",
        )
        PaymentFact.objects.create(
            external_id="case-test-2",
            date=date(2026, 1, 21), organization=self.organization,
            article=self.article_loan_in, counterparty=self.lender, contract=loan_contract,
            amount=Decimal("500000.00"), currency=self.rub,
            accounting_kind=AccountingKind.BU, direction=PaymentDirection.INFLOW,
            account="51",
        )
        # Оплата поставщику
        PaymentFact.objects.create(
            external_id="case-test-3",
            date=date(2026, 1, 25), organization=self.organization,
            article=self.article_out, counterparty=self.supplier, contract=supplier_contract,
            amount=Decimal("2500000.00"), currency=self.rub,
            accounting_kind=AccountingKind.BU, direction=PaymentDirection.OUTFLOW,
            account="51",
        )

        overview = build_case_overview(case)
        # Пришло всего: 2M + 500k = 2.5M
        self.assertEqual(overview["total_inflow"], Decimal("2500000.00"))
        # Ушло: 2.5M
        self.assertEqual(overview["total_outflow"], Decimal("2500000.00"))
        # Доступно сейчас: 0
        self.assertEqual(overview["available_now"], Decimal("0.00"))
        # По доходному договору мама ничего больше не должна — весь приход уже был
        # Но! Нужно ещё 500k + проценты для возврата займа
        # expected_outflow_remaining = 0 (поставщику доплатили) + 500k + 60k проценты (12% от 500k) = 560k
        self.assertGreater(overview["expected_outflow_remaining"], Decimal("500000.00"))
        # Прогноз сальдо: 0 + 0 (по приходу) − 560k = −560k → дефицит
        self.assertLess(overview["projected_balance"], Decimal("0.00"))
        self.assertEqual(overview["status_class"], "danger")

    def test_economist_can_create_funding_case_via_ui(self):
        self.client.login(username="economist", password="demo12345")
        # Установим working organization в сессии чтобы view принял дефолт
        session = self.client.session
        session["working_organization_id"] = self.organization.id
        session.save()
        response = self.client.post(
            reverse("funding_cases_index"),
            {
                "action": "create",
                "code": "FUND-DEMO-1",
                "name": "Тестовый кейс",
                "organization_id": self.organization.id,
                "description": "Описание",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(FundingCase.objects.filter(code="FUND-DEMO-1").exists())

    def test_case_detail_renders_with_attachable_contracts(self):
        case = FundingCase.objects.create(
            code="FUND-002", name="Пустой кейс",
            organization=self.organization, status=FundingCaseStatus.ACTIVE,
        )
        self.client.login(username="economist", password="demo12345")
        response = self.client.get(reverse("funding_case_detail", args=[case.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Пустой кейс")
        self.assertContains(response, "Привязать договор")
