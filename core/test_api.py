"""Тесты REST API v1 (token-based)."""

import json
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import (
    ApiToken,
    Currency,
    ExchangeRate,
    FundingCase,
    Organization,
)


class ApiTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="apiuser", password="x")
        self.token = ApiToken.objects.create(name="Test integration", user=self.user)
        self.org = Organization.objects.create(name="API Org", source_system="manual")

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Token {self.token.token}"}

    def test_api_requires_token(self):
        response = self.client.get("/api/v1/funding-cases/")
        self.assertEqual(response.status_code, 401)

    def test_api_rejects_invalid_token(self):
        response = self.client.get(
            "/api/v1/funding-cases/",
            HTTP_AUTHORIZATION="Token invalid-uuid-here",
        )
        self.assertEqual(response.status_code, 401)

    def test_api_root_returns_endpoint_list(self):
        response = self.client.get("/api/v1/", **self._auth())
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["version"], "v1")
        self.assertEqual(data["user"], "apiuser")
        self.assertIn("funding_cases_list", data["endpoints"])

    def test_funding_cases_list_with_filter(self):
        FundingCase.objects.create(
            code="API-1", name="Active case", organization=self.org, status="active",
        )
        FundingCase.objects.create(
            code="API-2", name="Closed case", organization=self.org, status="closed",
        )
        response = self.client.get("/api/v1/funding-cases/?status=active", **self._auth())
        data = json.loads(response.content)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["code"], "API-1")

    def test_funding_case_detail_returns_overview(self):
        case = FundingCase.objects.create(
            code="API-D", name="Detail case", organization=self.org,
        )
        response = self.client.get(f"/api/v1/funding-cases/{case.id}/", **self._auth())
        data = json.loads(response.content)
        self.assertEqual(data["code"], "API-D")
        self.assertIn("totals", data)
        self.assertIn("risk_score", data)
        self.assertIn("alerts", data)

    def test_exchange_rates_endpoint(self):
        usd = Currency.objects.create(code="USD", name="USD", source_system="manual")
        ExchangeRate.objects.create(
            currency=usd, rate_date=date(2026, 6, 1),
            rate_to_rub=Decimal("95.47"), source="manual",
        )
        response = self.client.get("/api/v1/exchange-rates/?currency=USD", **self._auth())
        data = json.loads(response.content)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["currency"], "USD")
        self.assertEqual(data["results"][0]["rate_to_rub"], "95.470000")

    def test_revoked_token_rejected(self):
        self.token.is_active = False
        self.token.save()
        response = self.client.get("/api/v1/funding-cases/", **self._auth())
        self.assertEqual(response.status_code, 401)

    def test_last_used_at_updated(self):
        self.assertIsNone(self.token.last_used_at)
        self.client.get("/api/v1/", **self._auth())
        self.token.refresh_from_db()
        self.assertIsNotNone(self.token.last_used_at)
