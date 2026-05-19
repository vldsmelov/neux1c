from django.db import OperationalError, ProgrammingError

from .models import AuditAction, AuditLog


class SecurityHeadersMiddleware:
    """Adds lightweight browser security headers without extra dependencies."""

    CONTENT_SECURITY_POLICY = (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'; "
        "form-action 'self'"
    )

    PERMISSIONS_POLICY = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
        "fullscreen=(self)"
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault("Content-Security-Policy", self.CONTENT_SECURITY_POLICY)
        response.setdefault("Permissions-Policy", self.PERMISSIONS_POLICY)
        response.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        return response


class AuditLogMiddleware:
    SKIPPED_PREFIXES = ("/admin/jsi18n/", "/healthz/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        self._write_log(request, response)
        return response

    def _write_log(self, request, response) -> None:
        if any(request.path.startswith(prefix) for prefix in self.SKIPPED_PREFIXES):
            return
        if not getattr(request, "user", None) or not request.user.is_authenticated:
            return

        action = AuditAction.VIEW
        if response.status_code in (401, 403):
            action = AuditAction.DENIED
        elif request.method == "POST":
            action = AuditAction.UPDATE

        try:
            AuditLog.objects.create(
                user=request.user,
                action=action,
                path=request.path[:255],
                method=request.method,
                status_code=response.status_code,
                ip_address=_get_client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:255],
            )
        except (OperationalError, ProgrammingError):
            return


def _get_client_ip(request):
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")
