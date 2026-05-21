import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.services.access_control import setup_demo_users, setup_role_groups


WEAK_DEMO_PASSWORDS = {"demo12345", "password", "12345", "admin"}


class Command(BaseCommand):
    help = "Create RBAC groups and optional demo users for the pilot."

    def add_arguments(self, parser):
        parser.add_argument(
            "--with-users",
            action="store_true",
            help="Create demo users: admin, economist, manager, accountant.",
        )
        parser.add_argument(
            "--password",
            default=os.getenv("NE_UX_DEMO_PASSWORD", "demo12345"),
            help="Password for demo users.",
        )
        parser.add_argument(
            "--allow-weak-password",
            action="store_true",
            help="Bypass production weak-password guard (use only for explicit local seeding).",
        )

    def handle(self, *args, **options):
        stats = setup_role_groups()
        should_print = options.get("verbosity", 1) > 0
        if should_print:
            self.stdout.write(self.style.SUCCESS(f"RBAC groups configured: {stats}"))

        if options["with_users"]:
            password = options["password"]
            if (
                not settings.DEBUG
                and not getattr(settings, "TESTING", False)
                and not options["allow_weak_password"]
                and password.lower() in WEAK_DEMO_PASSWORDS
            ):
                raise CommandError(
                    "Отказ создавать демо-пользователей с дефолтным паролем при DJANGO_DEBUG=0. "
                    "Задайте NE_UX_DEMO_PASSWORD/--password или передайте --allow-weak-password явно."
                )
            users = setup_demo_users(password)
            if should_print:
                self.stdout.write(self.style.SUCCESS(f"Demo users configured: {', '.join(users)}"))
