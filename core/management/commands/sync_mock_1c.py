from django.core.management.base import BaseCommand

from core.integrations.one_c.mock import MockOneCProvider
from core.services.one_c_sync import sync_one_c_dataset


class Command(BaseCommand):
    help = "Load pilot reference data and payment facts from the Mock-1С provider."

    def handle(self, *args, **options):
        run = sync_one_c_dataset(MockOneCProvider())
        self.stdout.write(self.style.SUCCESS(run.message))
        self.stdout.write(str(run.stats))
