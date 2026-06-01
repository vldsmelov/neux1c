"""Страница курсов валют /settings/exchange-rates/."""

from datetime import date as date_cls
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from ..access import role_required
from ..models import Currency, ExchangeRate, UserRole
from ..services.exchange_rates import latest_rates_summary
from ._shared import parse_optional_int


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def exchange_rates(request):
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "create":
                _create_rate(request)
                messages.success(request, "Курс добавлен")
            elif action == "delete":
                rate = get_object_or_404(
                    ExchangeRate, pk=parse_optional_int(request.POST.get("rate_id"))
                )
                rate_str = str(rate)
                rate.delete()
                messages.success(request, f"Курс удалён: {rate_str}")
        except (ValueError, InvalidOperation) as exc:
            messages.error(request, str(exc))
        return redirect("exchange_rates")

    selected_currency_id = parse_optional_int(request.GET.get("currency_id"))
    rates_qs = ExchangeRate.objects.select_related("currency", "created_by").order_by(
        "-rate_date", "currency__code"
    )
    if selected_currency_id:
        rates_qs = rates_qs.filter(currency_id=selected_currency_id)

    return render(
        request,
        "core/exchange_rates.html",
        {
            "active_section": "settings",
            "summary": latest_rates_summary(),
            "currencies": Currency.objects.exclude(code="RUB").order_by("code"),
            "selected_currency_id": selected_currency_id,
            "rates": rates_qs[:100],
            "today": timezone.localdate(),
        },
    )


def _create_rate(request) -> ExchangeRate:
    currency = get_object_or_404(
        Currency, pk=parse_optional_int(request.POST.get("currency_id"))
    )
    if currency.code == "RUB":
        raise ValueError("Курс RUB к RUB не имеет смысла")
    raw_date = (request.POST.get("rate_date") or "").strip()
    if not raw_date:
        raise ValueError("Укажите дату курса")
    try:
        rate_date = date_cls.fromisoformat(raw_date)
    except ValueError as exc:
        raise ValueError(f"Некорректная дата: {raw_date}") from exc
    raw_rate = (request.POST.get("rate_to_rub") or "").strip().replace(",", ".")
    try:
        rate_val = Decimal(raw_rate)
    except InvalidOperation as exc:
        raise ValueError(f"Некорректный курс: {raw_rate}") from exc
    if rate_val <= 0:
        raise ValueError("Курс должен быть больше нуля")
    source = request.POST.get("source") or ExchangeRate.SOURCE_MANUAL
    if source not in {s for s, _ in ExchangeRate.SOURCE_CHOICES}:
        source = ExchangeRate.SOURCE_MANUAL

    rate, created = ExchangeRate.objects.update_or_create(
        currency=currency,
        rate_date=rate_date,
        source=source,
        defaults={
            "rate_to_rub": rate_val,
            "comment": (request.POST.get("comment") or "").strip()[:255],
            "created_by": request.user,
        },
    )
    return rate
