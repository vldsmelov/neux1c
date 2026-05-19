from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import DocumentSequence


@transaction.atomic
def next_document_number(prefix: str, *, year: int | None = None, width: int = 6) -> str:
    year = year or timezone.localdate().year
    sequence = _get_locked_sequence(prefix=prefix, year=year)
    sequence.current_value += 1
    sequence.save(update_fields=["current_value", "updated_at"])
    return f"{prefix}-{year}-{sequence.current_value:0{width}d}"


def _get_locked_sequence(*, prefix: str, year: int) -> DocumentSequence:
    try:
        sequence, _ = DocumentSequence.objects.select_for_update().get_or_create(
            prefix=prefix,
            year=year,
            defaults={"current_value": 0},
        )
        return sequence
    except IntegrityError:
        return DocumentSequence.objects.select_for_update().get(prefix=prefix, year=year)
