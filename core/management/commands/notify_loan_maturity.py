"""Запускается планировщиком: ищет договоры займа кейсов, у которых
maturity_date ≤ сегодня + N дней, и отправляет owner-у кейса уведомление
«приближается срок возврата». Запускать раз в день."""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import (
    Contract,
    ContractKind,
    FundingCaseStatus,
    Notification,
    NotificationKind,
)
from core.services.funding_cases import build_case_overview
from core.services.notifications import notify


class Command(BaseCommand):
    help = "Напомнить ответственным о приближающихся сроках возврата займов в активных кейсах"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=7, help="За сколько дней начинать напоминать")
        parser.add_argument("--dry-run", action="store_true", help="Не отправлять, только показать")

    def handle(self, *args, **options):
        days = options["days"]
        dry_run = options["dry_run"]
        today = timezone.localdate()
        cutoff = today + timedelta(days=days)

        loans = (
            Contract.objects.filter(
                kind=ContractKind.LOAN_RECEIVED,
                funding_case__isnull=False,
                funding_case__status=FundingCaseStatus.ACTIVE,
                maturity_date__isnull=False,
                maturity_date__lte=cutoff,
                maturity_date__gte=today,
            )
            .select_related("funding_case", "funding_case__owner", "counterparty")
        )

        sent = 0
        for loan in loans:
            case = loan.funding_case
            if not case.owner_id:
                continue
            # Не спамим — если такое уведомление уже было за последние 24 часа, пропускаем
            recent = Notification.objects.filter(
                recipient=case.owner,
                kind=NotificationKind.LOAN_MATURITY_NEAR,
                payload_object_id=str(loan.pk),
                created_at__gte=timezone.now() - timedelta(hours=24),
            ).exists()
            if recent:
                continue

            overview = build_case_overview(case)
            stat = None
            for s in overview["contracts_by_kind"].get(ContractKind.LOAN_RECEIVED, []):
                if s.contract.id == loan.id:
                    stat = s
                    break

            days_left = (loan.maturity_date - today).days
            balance = stat.expected_remaining if stat else loan.amount
            title = f"Возврат займа {loan.number}: {days_left} дн. до {loan.maturity_date:%d.%m.%Y}"
            text = (
                f"Кейс «{case.code} · {case.name}» · кредитор {loan.counterparty.name} · "
                f"остаток к возврату: {balance} ₽"
            )

            if dry_run:
                self.stdout.write(f"[dry-run] → {case.owner.username}: {title} | {text}")
                continue

            notify(
                recipient=case.owner,
                kind=NotificationKind.LOAN_MATURITY_NEAR,
                title=title,
                text=text,
                link=f"/funding/cases/{case.pk}/",
                related=loan,
            )
            sent += 1
            self.stdout.write(f"→ {case.owner.username}: {title}")

        self.stdout.write(self.style.SUCCESS(f"Отправлено уведомлений: {sent}"))
