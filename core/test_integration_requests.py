from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from core.models import IntegrationRequest, IntegrationRequestStatus


class IntegrationRequestsTests(TestCase):
    def setUp(self):
        call_command("setup_access_roles", "--with-users", verbosity=0)
        User = get_user_model()
        self.admin = User.objects.get(username="admin")
        self.economist = User.objects.get(username="economist")
        self.manager = User.objects.get(username="manager")

    def test_economist_can_create_integration_request(self):
        self.client.login(username="economist", password="demo12345")

        response = self.client.post(
            reverse("integration_requests"),
            {
                "action": "create",
                "integration_name": "Интеграция закупок",
                "target_system": "1С:ERP",
                "description": "Нужен обмен заявками на закупку и статусами согласования",
            },
        )

        self.assertRedirects(response, reverse("integration_requests"))
        self.assertEqual(IntegrationRequest.objects.count(), 1)
        request = IntegrationRequest.objects.first()
        self.assertEqual(request.status, IntegrationRequestStatus.NEW)
        self.assertEqual(request.requested_by, self.economist)

    def test_admin_can_update_request_status(self):
        request = IntegrationRequest.objects.create(
            number="INT-2026-000001",
            requested_by=self.economist,
            integration_name="Интеграция БДР",
            target_system="1С:УХ",
            description="Нужна синхронизация данных БДР",
        )
        self.client.login(username="admin", password="demo12345")

        response = self.client.post(
            reverse("integration_requests"),
            {
                "action": "set_status",
                "request_id": request.id,
                "status": IntegrationRequestStatus.IN_PROGRESS,
                "admin_comment": "Взято в работу",
            },
        )

        self.assertRedirects(response, reverse("integration_requests"))
        request.refresh_from_db()
        self.assertEqual(request.status, IntegrationRequestStatus.IN_PROGRESS)
        self.assertEqual(request.assigned_admin, self.admin)
        self.assertEqual(request.admin_comment, "Взято в работу")

    def test_manager_cannot_update_request_status(self):
        request = IntegrationRequest.objects.create(
            number="INT-2026-000001",
            requested_by=self.economist,
            integration_name="Интеграция БДР",
            target_system="1С:УХ",
            description="Нужна синхронизация данных БДР",
        )
        self.client.login(username="manager", password="demo12345")

        response = self.client.post(
            reverse("integration_requests"),
            {
                "action": "set_status",
                "request_id": request.id,
                "status": IntegrationRequestStatus.COMPLETED,
                "admin_comment": "Пытаюсь закрыть",
            },
        )

        self.assertRedirects(response, reverse("integration_requests"))
        request.refresh_from_db()
        self.assertEqual(request.status, IntegrationRequestStatus.NEW)
        self.assertIsNone(request.assigned_admin)
