"""Notify approvers about payment requests overdue on the approval SLA.

Intended to run on a daily cron:

    docker compose exec web python manage.py send_pending_sla_digest

For every approver who has at least one PENDING_APPROVAL request older than
the SLA window, create a single digest notification (one per approver, not
one per request — to avoid spamming). Idempotent within a day: re-running
simply creates a fresh digest reflecting the current state, so it's safe to
schedule.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count, Min
from django.utils import timezone

from core.models import NotificationKind, PaymentRequest, PaymentRequestStatus
from core.services.manager_dashboard import PENDING_SLA_DAYS
from core.services.notifications import notify


class Command(BaseCommand):
    help = "Send each approver a digest of their overdue pending payment requests."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=PENDING_SLA_DAYS,
            help=f"SLA window in days (default {PENDING_SLA_DAYS}).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be sent without creating notifications.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        dry_run = options["dry_run"]
        cutoff = timezone.now() - timedelta(days=days)

        overdue = (
            PaymentRequest.objects.filter(
                status=PaymentRequestStatus.PENDING_APPROVAL,
                submitted_at__lt=cutoff,
                approver__isnull=False,
            )
            .values("approver_id")
            .annotate(total=Count("id"), oldest=Min("submitted_at"))
        )

        sent = 0
        for row in overdue:
            approver_id = row["approver_id"]
            total = row["total"]
            oldest = row["oldest"]
            oldest_days = (timezone.now() - oldest).days if oldest else days
            title = f"Просрочено заявок на согласовании: {total}"
            text = (
                f"Самая старая ждёт уже {oldest_days} дн. "
                f"Откройте журнал заявок и обработайте очередь."
            )
            if dry_run:
                self.stdout.write(f"[dry-run] approver={approver_id}: {total} overdue, oldest {oldest_days}d")
                continue

            from django.contrib.auth import get_user_model
            approver = get_user_model().objects.filter(pk=approver_id).first()
            if approver is None:
                continue
            notify(
                recipient=approver,
                kind=NotificationKind.LIMIT_OVERRUN,  # reuse a high-attention kind
                title=title,
                text=text,
                link="/payments/requests/?status=pending_approval",
            )
            sent += 1

        if options.get("verbosity", 1) > 0:
            verb = "Would notify" if dry_run else "Notified"
            self.stdout.write(self.style.SUCCESS(f"{verb} approvers: {sent if not dry_run else len(list(overdue))}"))
