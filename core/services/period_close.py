"""Period Close — закрытие учётного периода (enterprise feature).

Закрытие защищает финансовый периметр после сверки факта с банком и
выгрузки в БУ/НУ. После закрытия:
* любые попытки создать/изменить факт оплаты в закрытом периоде блокируются;
* любые попытки создать корректировку факта в закрытом периоде блокируются;
* для исправлений нужно явно открыть период обратно (с указанием причины).
"""

from datetime import date as date_cls

from django.utils import timezone

from core.models import AccountingPeriodLock, Organization


class PeriodLockedError(Exception):
    """Период закрыт — операция не выполняется."""


def is_period_locked(organization: Organization | int | None, target_date: date_cls) -> bool:
    """True если для (organization, year, month) есть активный lock."""
    if organization is None:
        return False
    org_id = organization.pk if isinstance(organization, Organization) else organization
    return AccountingPeriodLock.objects.filter(
        organization_id=org_id,
        year=target_date.year,
        month=target_date.month,
        unlocked_at__isnull=True,
    ).exists()


def assert_period_open(organization, target_date: date_cls) -> None:
    """Бросает PeriodLockedError если период закрыт. Вызывать перед сохранением."""
    if is_period_locked(organization, target_date):
        raise PeriodLockedError(
            f"Период {target_date.year}-{target_date.month:02d} закрыт для "
            f"{getattr(organization, 'name', organization)}. "
            f"Финансовый директор должен открыть период для исправлений."
        )


def close_period(*, organization: Organization, year: int, month: int, user, comment: str = "") -> AccountingPeriodLock:
    """Закрывает период для (organization, year, month). Идемпотентно."""
    if not (1 <= month <= 12):
        raise ValueError("Месяц должен быть от 1 до 12")
    existing = AccountingPeriodLock.objects.filter(
        organization=organization, year=year, month=month
    ).first()
    if existing and existing.unlocked_at is None:
        return existing
    if existing:
        # Период был открыт — переоткрываем через создание нового lock?
        # Нет — переиспользуем запись, сбрасывая unlocked_at.
        existing.unlocked_at = None
        existing.unlocked_by = None
        existing.unlock_reason = ""
        existing.locked_by = user
        existing.locked_at = timezone.now()
        existing.comment = comment
        existing.save()
        return existing
    return AccountingPeriodLock.objects.create(
        organization=organization,
        year=year,
        month=month,
        locked_by=user,
        comment=comment,
    )


def reopen_period(lock: AccountingPeriodLock, user, reason: str) -> AccountingPeriodLock:
    """Открывает закрытый период для правок. Требует указания причины."""
    if not reason.strip():
        raise ValueError("Укажите причину повторного открытия периода")
    lock.unlocked_at = timezone.now()
    lock.unlocked_by = user
    lock.unlock_reason = reason.strip()[:255]
    lock.save(update_fields=["unlocked_at", "unlocked_by", "unlock_reason"])
    return lock


def organization_lock_summary(organization: Organization) -> list[dict]:
    """Сводка по периодам для одной организации: для UI страницы закрытия."""
    locks = AccountingPeriodLock.objects.filter(organization=organization).select_related(
        "locked_by", "unlocked_by"
    )
    locks_map = {(lock.year, lock.month): lock for lock in locks}

    today = timezone.localdate()
    # Показываем 12 последних месяцев + текущий
    rows: list[dict] = []
    year, month = today.year, today.month
    for _ in range(13):
        key = (year, month)
        lock = locks_map.get(key)
        rows.append({
            "year": year,
            "month": month,
            "label": _month_label(year, month),
            "lock": lock,
            "is_locked": lock is not None and lock.is_active,
            "was_reopened": lock is not None and lock.unlocked_at is not None,
            "is_current_or_future": (year > today.year) or (year == today.year and month >= today.month),
        })
        month -= 1
        if month < 1:
            month = 12
            year -= 1
    return rows


def _month_label(year: int, month: int) -> str:
    months = ["январь", "февраль", "март", "апрель", "май", "июнь",
              "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
    return f"{months[month-1]} {year}"
