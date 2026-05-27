"""Notifications service — fan-out events from domain services to users.

Каждый вызов `notify(...)` создаёт запись в таблице Notification, которую
пользователь увидит как бейдж непрочитанных в шапке и на странице
`/notifications/`. Сервис тонкий: проверяет dedup, режет длинные поля,
безопасно игнорирует попытки уведомить системного/анонимного пользователя.
"""

from __future__ import annotations

from typing import Optional

from core.models import Notification, NotificationKind


def notify(
    *,
    recipient,
    kind: str,
    title: str,
    text: str = "",
    link: str = "",
    related=None,
) -> Optional[Notification]:
    """Create a single notification for `recipient`.

    `related` is an arbitrary model instance; we record its class name and pk
    so the UI/audit can correlate notifications back to the source document.
    Returns the created Notification, or None if the recipient is missing or
    the kind is unknown.
    """
    if recipient is None or getattr(recipient, "pk", None) is None:
        return None
    if kind not in NotificationKind.values:
        return None

    payload_object_type = ""
    payload_object_id = ""
    if related is not None:
        payload_object_type = related.__class__.__name__
        payload_object_id = str(getattr(related, "pk", "") or "")

    return Notification.objects.create(
        recipient=recipient,
        kind=kind,
        title=title[:200],
        text=text[:500],
        link=link[:255],
        payload_object_type=payload_object_type[:120],
        payload_object_id=payload_object_id[:120],
    )


def notify_many(recipients, **kwargs) -> int:
    """Fan-out to multiple recipients; returns number of notifications created."""
    count = 0
    seen_ids = set()
    for recipient in recipients:
        if recipient is None or recipient.pk in seen_ids:
            continue
        seen_ids.add(recipient.pk)
        if notify(recipient=recipient, **kwargs) is not None:
            count += 1
    return count


def mark_read(*, user, notification_ids=None) -> int:
    """Mark notifications as read. If `notification_ids` is None — mark all."""
    from django.utils import timezone

    query = Notification.objects.filter(recipient=user, read_at__isnull=True)
    if notification_ids is not None:
        query = query.filter(id__in=list(notification_ids))
    return query.update(read_at=timezone.now())


def unread_count(user) -> int:
    if not user or not user.is_authenticated:
        return 0
    return Notification.objects.filter(recipient=user, read_at__isnull=True).count()
