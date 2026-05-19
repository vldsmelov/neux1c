from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError


def payment_request_justification_upload_to(instance, filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return f"payment_requests/justifications/{uuid4().hex}{suffix}"


def validate_justification_file(uploaded_file) -> None:
    if not uploaded_file:
        return

    max_size = settings.NE_UX_MAX_JUSTIFICATION_FILE_SIZE
    if uploaded_file.size > max_size:
        max_size_mb = max_size / 1024 / 1024
        raise ValidationError(f"Файл обоснования не должен превышать {max_size_mb:.0f} МБ")

    extension = Path(uploaded_file.name).suffix.lower()
    allowed_extensions = {item.lower() for item in settings.NE_UX_ALLOWED_JUSTIFICATION_EXTENSIONS}
    if extension not in allowed_extensions:
        allowed = ", ".join(sorted(allowed_extensions))
        raise ValidationError(f"Недопустимый тип файла обоснования. Разрешены: {allowed}")

    content_type = getattr(uploaded_file, "content_type", "")
    allowed_content_types = set(settings.NE_UX_ALLOWED_JUSTIFICATION_CONTENT_TYPES)
    if content_type and content_type not in allowed_content_types:
        raise ValidationError("MIME-тип файла обоснования не разрешен")
