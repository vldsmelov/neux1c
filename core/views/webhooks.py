"""UI управления webhook-подписками /settings/webhooks/."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from ..models import WebhookSubscription
from ._shared import parse_optional_int


@login_required
def webhooks_index(request):
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "create":
                name = (request.POST.get("name") or "").strip()
                event_type = (request.POST.get("event_type") or "").strip()
                target_url = (request.POST.get("target_url") or "").strip()
                if not name or not target_url:
                    raise ValueError("Укажите название и URL")
                if event_type not in {e for e, _ in WebhookSubscription.EVENT_CHOICES}:
                    raise ValueError("Выберите событие")
                if not target_url.startswith(("http://", "https://")):
                    raise ValueError("URL должен начинаться с http:// или https://")
                hook = WebhookSubscription.objects.create(
                    name=name,
                    user=request.user,
                    event_type=event_type,
                    target_url=target_url,
                )
                messages.success(
                    request,
                    f"Webhook «{hook.name}» создан. Secret для HMAC-проверки: {hook.secret}",
                )
            elif action == "toggle":
                hook = get_object_or_404(
                    WebhookSubscription,
                    pk=parse_optional_int(request.POST.get("hook_id")),
                    user=request.user,
                )
                hook.is_active = not hook.is_active
                hook.failure_count = 0
                hook.save(update_fields=["is_active", "failure_count"])
                messages.success(
                    request,
                    f"Webhook «{hook.name}» {'активирован' if hook.is_active else 'отключён'}",
                )
            elif action == "delete":
                hook = get_object_or_404(
                    WebhookSubscription,
                    pk=parse_optional_int(request.POST.get("hook_id")),
                    user=request.user,
                )
                name = hook.name
                hook.delete()
                messages.success(request, f"Webhook «{name}» удалён")
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect("webhooks_index")

    hooks = WebhookSubscription.objects.filter(user=request.user).order_by("-created_at")
    return render(
        request,
        "core/webhooks.html",
        {
            "active_section": "settings",
            "hooks": hooks,
            "event_choices": WebhookSubscription.EVENT_CHOICES,
        },
    )
