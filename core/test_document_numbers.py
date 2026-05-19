from django.test import TestCase

from core.services.document_numbers import next_document_number


class DocumentNumberTests(TestCase):
    def test_next_document_number_increments_per_prefix_and_year(self):
        first = next_document_number("QA", year=2026)
        second = next_document_number("QA", year=2026)
        next_year = next_document_number("QA", year=2027)
        other_prefix = next_document_number("QB", year=2026)

        self.assertEqual(first, "QA-2026-000001")
        self.assertEqual(second, "QA-2026-000002")
        self.assertEqual(next_year, "QA-2027-000001")
        self.assertEqual(other_prefix, "QB-2026-000001")
