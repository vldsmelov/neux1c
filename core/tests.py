from decimal import Decimal

from django.test import TestCase

from core.integrations.one_c.mock import MockOneCProvider
from core.models import CashFlowArticle, Contract, ContractKind, PaymentFact, UiThemeSettings
from core.services.one_c_sync import sync_one_c_dataset


class MockOneCSyncTests(TestCase):
    def test_sync_is_idempotent(self):
        provider = MockOneCProvider()

        first_run = sync_one_c_dataset(provider)
        second_run = sync_one_c_dataset(provider)

        self.assertEqual(first_run.stats["created"]["cash_flow_articles"], 5)
        self.assertEqual(second_run.stats["updated"]["cash_flow_articles"], 5)
        self.assertEqual(CashFlowArticle.objects.count(), 5)
        self.assertEqual(Contract.objects.count(), 4)
        self.assertEqual(PaymentFact.objects.count(), 4)

    def test_article_flags_are_loaded(self):
        sync_one_c_dataset(MockOneCProvider())

        self.assertTrue(CashFlowArticle.objects.get(code="DDS-900").is_internal_turnover)
        self.assertFalse(CashFlowArticle.objects.get(code="DDS-MANUAL-001").exists_in_one_c)

    def test_supplier_contract_reserve_includes_additional_agreements(self):
        sync_one_c_dataset(MockOneCProvider())

        contract = Contract.objects.get(kind=ContractKind.SOLE_SUPPLIER, number="ЕП-2026-014")

        self.assertEqual(contract.reserved_amount, Decimal("3860000.00"))


class UiThemeSettingsTests(TestCase):
    def test_active_theme_exposes_css_variables(self):
        theme = UiThemeSettings.objects.create(
            name="Custom",
            is_active=True,
            primary_color="#006ecb",
        )

        variables = theme.as_css_variables()

        self.assertEqual(variables["--app-primary"], "#006ecb")
        self.assertIn("--app-bg", variables)

    def test_only_one_theme_stays_active(self):
        first = UiThemeSettings.objects.create(name="First", is_active=True)
        second = UiThemeSettings.objects.create(name="Second", is_active=True)

        first.refresh_from_db()
        second.refresh_from_db()

        self.assertFalse(first.is_active)
        self.assertTrue(second.is_active)
