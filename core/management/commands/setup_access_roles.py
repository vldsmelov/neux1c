import os

from django.core.management.base import BaseCommand

from core.services.access_control import setup_demo_users, setup_role_groups


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

    def handle(self, *args, **options):
        stats = setup_role_groups()
        should_print = options.get("verbosity", 1) > 0
        if should_print:
            self.stdout.write(self.style.SUCCESS(f"RBAC groups configured: {stats}"))

        if options["with_users"]:
            users = setup_demo_users(options["password"])
            if should_print:
                self.stdout.write(self.style.SUCCESS(f"Demo users configured: {', '.join(users)}"))
