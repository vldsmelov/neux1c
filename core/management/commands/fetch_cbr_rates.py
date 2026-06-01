"""Загрузка курсов валют ЦБ РФ.

Запускается планировщиком (cron) ежедневно:
    docker compose exec web python manage.py fetch_cbr_rates

С указанной датой:
    python manage.py fetch_cbr_rates --date 2026-05-15

Курсы создаются с source='cbr' — не конфликтуют с ручными курсами,
система учитывает оба источника.
"""

from datetime import date as date_cls

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.services.exchange_rates import fetch_cbr_rates


class Command(BaseCommand):
    help = "Загрузить курсы валют ЦБ РФ на дату (по умолчанию — сегодня)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            help="Дата в формате YYYY-MM-DD (по умолчанию — сегодня)",
        )

    def handle(self, *args, **options):
        raw_date = options.get("date")
        if raw_date:
            try:
                target_date = date_cls.fromisoformat(raw_date)
            except ValueError as exc:
                self.stderr.write(self.style.ERROR(f"Bad --date: {exc}"))
                return
        else:
            target_date = timezone.localdate()

        self.stdout.write(f"Загружаю курсы ЦБ РФ на {target_date:%d.%m.%Y}...")
        result = fetch_cbr_rates(target_date)

        if result["errors"]:
            for err in result["errors"]:
                self.stderr.write(self.style.ERROR(f"  ⚠ {err}"))

        msg = (
            f"Готово: создано {result['created']}, обновлено {result['updated']}, "
            f"пропущено {result['skipped']} (не в нашем справочнике валют)"
        )
        style = self.style.SUCCESS if not result["errors"] else self.style.WARNING
        self.stdout.write(style(msg))
