"""UI управления API-токенами /settings/api-tokens/."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from ..models import ApiToken
from ._shared import parse_optional_int


@login_required
def api_tokens(request):
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "create":
                name = (request.POST.get("name") or "").strip()
                if not name:
                    raise ValueError("Укажите название токена")
                token = ApiToken.objects.create(name=name, user=request.user)
                messages.success(
                    request,
                    f"Токен «{token.name}» создан. Скопируйте его сейчас — больше показан не будет: {token.token}",
                )
            elif action == "revoke":
                token = get_object_or_404(
                    ApiToken,
                    pk=parse_optional_int(request.POST.get("token_id")),
                    user=request.user,
                )
                token.is_active = False
                token.save(update_fields=["is_active"])
                messages.success(request, f"Токен «{token.name}» отозван")
            elif action == "delete":
                token = get_object_or_404(
                    ApiToken,
                    pk=parse_optional_int(request.POST.get("token_id")),
                    user=request.user,
                )
                name = token.name
                token.delete()
                messages.success(request, f"Токен «{name}» удалён")
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect("api_tokens")

    tokens = ApiToken.objects.filter(user=request.user).order_by("-created_at")

    return render(
        request,
        "core/api_tokens.html",
        {
            "active_section": "settings",
            "tokens": tokens,
        },
    )
