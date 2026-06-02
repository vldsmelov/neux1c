"""Outbound webhooks — отправка POST в зарегистрированные внешние системы."""

import hashlib
import hmac
import json

from django.utils import timezone

from core.models import WebhookSubscription


def fire_webhook(event_type: str, payload: dict, *, http_poster=None) -> int:
    """Шлёт POST на все активные подписки по этому событию.

    http_poster — функция (url, body_bytes, headers) → status_code,
    для тестов можно подменить (stdlib не имеет POST по умолчанию).

    Возвращает количество успешных отправок.
    """
    subscriptions = WebhookSubscription.objects.filter(
        event_type=event_type, is_active=True
    )
    if not subscriptions.exists():
        return 0

    if http_poster is None:
        http_poster = _default_http_poster

    body_str = json.dumps({
        "event": event_type,
        "timestamp": timezone.now().isoformat(),
        "data": payload,
    })
    body_bytes = body_str.encode("utf-8")

    success = 0
    for sub in subscriptions:
        signature = hmac.new(
            sub.secret.encode("utf-8"), body_bytes, hashlib.sha256
        ).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-NEUX-Signature": signature,
            "X-NEUX-Event": event_type,
            "User-Agent": "NE-UX-Webhook/1.0",
        }
        try:
            status = http_poster(sub.target_url, body_bytes, headers)
        except Exception:
            status = 0
        sub.last_fired_at = timezone.now()
        sub.last_status_code = status
        if 200 <= status < 300:
            sub.failure_count = 0
            success += 1
        else:
            sub.failure_count += 1
            # Авто-disable после 10 подряд ошибок — стандартный паттерн
            # webhook-провайдеров (Stripe, GitHub) для защиты от спама на
            # сдохший endpoint.
            if sub.failure_count >= 10:
                sub.is_active = False
        sub.save(update_fields=["last_fired_at", "last_status_code", "failure_count", "is_active"])

    return success


def _default_http_poster(url: str, body: bytes, headers: dict) -> int:
    """Дефолтный poster через stdlib urllib."""
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    req = Request(url, data=body, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status
    except HTTPError as exc:
        return exc.code
    except URLError:
        return 0
