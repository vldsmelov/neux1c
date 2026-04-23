from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render


def workspace(request):
    modules = [
        {"name": "Планирование и контроль", "state": "каркас"},
        {"name": "НСИ и договоры", "state": "следующая итерация"},
        {"name": "Mock-1C", "state": "проектируется"},
        {"name": "Отчетность", "state": "ожидает данных"},
    ]
    return render(request, "core/workspace.html", {"modules": modules})


def healthz(request):
    database = "ok"
    try:
        connection.ensure_connection()
    except Exception:
        database = "error"

    status = 200 if database == "ok" else 503
    return JsonResponse({"status": "ok" if status == 200 else "error", "database": database}, status=status)
