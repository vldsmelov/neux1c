"""Тесты Webhooks: HMAC, fire, auto-disable."""

import hashlib
import hmac
import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import WebhookSubscription
from core.services.webhooks import fire_webhook


class WebhookTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="wh", password="x")

    def _make_sub(self, **kw):
        defaults = dict(
            name="Test", user=self.user,
            event_type=WebhookSubscription.EVENT_CASE_STATUS,
            target_url="http://example.com/hook",
        )
        defaults.update(kw)
        return WebhookSubscription.objects.create(**defaults)

    def test_fire_webhook_calls_poster_with_signed_body(self):
        sub = self._make_sub()
        captured = {}

        def fake_poster(url, body, headers):
            captured["url"] = url
            captured["body"] = body
            captured["headers"] = dict(headers)
            return 200

        success = fire_webhook(
            WebhookSubscription.EVENT_CASE_STATUS,
            {"case_id": 1, "code": "FUND-T1"},
            http_poster=fake_poster,
        )
        self.assertEqual(success, 1)
        self.assertEqual(captured["url"], "http://example.com/hook")

        # Подпись HMAC валидна
        expected_sig = hmac.new(
            sub.secret.encode(), captured["body"], hashlib.sha256
        ).hexdigest()
        self.assertEqual(captured["headers"]["X-NEUX-Signature"], expected_sig)
        self.assertEqual(captured["headers"]["X-NEUX-Event"], WebhookSubscription.EVENT_CASE_STATUS)

        # Payload содержит event + timestamp + data
        payload = json.loads(captured["body"])
        self.assertEqual(payload["event"], WebhookSubscription.EVENT_CASE_STATUS)
        self.assertEqual(payload["data"]["case_id"], 1)
        self.assertIn("timestamp", payload)

        sub.refresh_from_db()
        self.assertEqual(sub.last_status_code, 200)
        self.assertEqual(sub.failure_count, 0)

    def test_fire_webhook_only_fires_matching_event(self):
        self._make_sub(event_type=WebhookSubscription.EVENT_CASE_STATUS)
        captured_count = [0]

        def fake_poster(url, body, headers):
            captured_count[0] += 1
            return 200

        fire_webhook(
            WebhookSubscription.EVENT_CASE_CREATED,
            {"case_id": 2},
            http_poster=fake_poster,
        )
        self.assertEqual(captured_count[0], 0)

    def test_failures_increment_and_auto_disable_at_10(self):
        sub = self._make_sub()

        def fake_500(url, body, headers):
            return 500

        for i in range(10):
            fire_webhook(
                WebhookSubscription.EVENT_CASE_STATUS,
                {"i": i},
                http_poster=fake_500,
            )
        sub.refresh_from_db()
        self.assertEqual(sub.failure_count, 10)
        self.assertFalse(sub.is_active)

    def test_skips_inactive_subscriptions(self):
        self._make_sub(is_active=False)
        count = [0]

        def fake_poster(url, body, headers):
            count[0] += 1
            return 200

        fire_webhook(
            WebhookSubscription.EVENT_CASE_STATUS,
            {},
            http_poster=fake_poster,
        )
        self.assertEqual(count[0], 0)
