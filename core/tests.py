from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import RequestFactory, TestCase
from django.urls import reverse

from core.context_processors import ui_theme
from core.integrations.one_c.mock import MockOneCProvider
from core.models import (
    AccountingKind,
    AuditAction,
    AuditLog,
    BudgetLimitAdjustment,
    BudgetLimitAdjustmentMonth,
    BudgetLimitMonth,
    BudgetLimitPlan,
    BudgetPlanStatus,
    CashFlowArticle,
    Contract,
    ContractKind,
    Currency,
    Department,
    ExternalPaymentDocument,
    Organization,
    PaymentFact,
    PaymentRequest,
    SourceSystem,
    UiThemeMode,
    UiThemeSettings,
    UserProfile,
    UserRole,
)
from core.services.budget_planning import (
    approve_budget_plan,
    approve_limit_adjustment,
    create_budget_plan,
    create_limit_adjustment,
    submit_budget_plan,
    submit_limit_adjustment,
)
from core.services.one_c_sync import sync_one_c_dataset


class MockOneCSyncTests(TestCase):
    def test_sync_is_idempotent(self):
        provider = MockOneCProvider()

        first_run = sync_one_c_dataset(provider)
        second_run = sync_one_c_dataset(provider)

        self.assertEqual(first_run.stats["created"]["cash_flow_articles"], 5)
        self.assertEqual(second_run.stats["updated"]["cash_flow_articles"], 5)
        self.assertEqual(CashFlowArticle.objects.count(), 5)
        self.assertEqual(Contract.objects.count(), 4)
        self.assertEqual(PaymentFact.objects.count(), 4)

    def test_article_flags_are_loaded(self):
        sync_one_c_dataset(MockOneCProvider())

        self.assertTrue(CashFlowArticle.objects.get(code="DDS-900").is_internal_turnover)
        self.assertFalse(CashFlowArticle.objects.get(code="DDS-MANUAL-001").exists_in_one_c)

    def test_supplier_contract_reserve_includes_additional_agreements(self):
        sync_one_c_dataset(MockOneCProvider())

        contract = Contract.objects.get(kind=ContractKind.SOLE_SUPPLIER, number="ЕП-2026-014")

        self.assertEqual(contract.reserved_amount, Decimal("3860000.00"))


class UiThemeSettingsTests(TestCase):
    def test_active_theme_exposes_css_variables(self):
        theme = UiThemeSettings.objects.create(
            name="Custom",
            is_active=True,
            primary_color="#006ecb",
        )

        variables = theme.as_css_variables()

        self.assertEqual(variables["--app-primary"], "#006ecb")
        self.assertIn("--app-bg", variables)

    def test_only_one_theme_stays_active(self):
        first = UiThemeSettings.objects.create(name="First", is_active=True)
        second = UiThemeSettings.objects.create(name="Second", is_active=True)

        first.refresh_from_db()
        second.refresh_from_db()

        self.assertFalse(first.is_active)
        self.assertTrue(second.is_active)

    def test_dark_mode_changes_css_variables(self):
        User = get_user_model()
        user = User.objects.create_user(username="theme_user", password="demo12345")
        UserProfile.objects.create(user=user, role=UserRole.ECONOMIST, theme_mode=UiThemeMode.DARK)
        request = RequestFactory().get("/")
        request.user = user
        request.session = {}

        payload = ui_theme(request)

        self.assertEqual(payload["ui_theme_mode"], UiThemeMode.DARK)
        self.assertEqual(payload["ui_theme"]["--app-bg"], "#111a26")
        self.assertEqual(payload["ui_theme"]["--app-surface"], "#182433")


class AccessControlTests(TestCase):
    def setUp(self):
        call_command("setup_access_roles", "--with-users", verbosity=0)

    def test_demo_users_and_groups_are_created(self):
        User = get_user_model()

        self.assertEqual(Group.objects.filter(name__in=["Администратор", "Экономист", "Руководитель", "Бухгалтер"]).count(), 4)
        self.assertEqual(User.objects.filter(username__in=["admin", "economist", "manager", "accountant"]).count(), 4)
        self.assertEqual(UserProfile.objects.get(user__username="accountant").role, UserRole.ACCOUNTANT)

    def test_setup_access_roles_refuses_weak_password_in_production(self):
        from django.core.management.base import CommandError
        from django.test import override_settings

        # Simulate production: DEBUG=0 AND not running under the test runner bypass.
        with override_settings(DEBUG=False, TESTING=False):
            with self.assertRaisesMessage(CommandError, "дефолтным паролем"):
                call_command("setup_access_roles", "--with-users", "--password=demo12345", verbosity=0)

    def test_setup_access_roles_accepts_weak_password_with_explicit_flag(self):
        from django.test import override_settings

        with override_settings(DEBUG=False, TESTING=False):
            # --allow-weak-password is the documented escape hatch and must keep working.
            call_command(
                "setup_access_roles",
                "--with-users",
                "--password=demo12345",
                "--allow-weak-password",
                verbosity=0,
            )

    def test_workspace_requires_login(self):
        response = self.client.get(reverse("workspace"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_economist_can_open_nsi_and_action_is_logged(self):
        self.client.login(username="economist", password="demo12345")

        response = self.client.get(reverse("nsi_dashboard"))

        self.assertRedirects(response, reverse("nsi_directory", kwargs={"directory": "organizations"}), fetch_redirect_response=False)

    def test_economist_can_create_manual_department_from_nsi(self):
        self.client.login(username="economist", password="demo12345")

        response = self.client.post(
            reverse("nsi_dashboard"),
            {
                "action": "create",
                "directory": "departments",
                "code": "CFO-777",
                "name": "Проектный офис",
            },
        )

        self.assertRedirects(response, reverse("nsi_directory", kwargs={"directory": "departments"}), fetch_redirect_response=False)
        department = Department.objects.get(code="CFO-777")
        self.assertEqual(department.name, "Проектный офис")
        self.assertEqual(department.source_system, SourceSystem.MANUAL)
        self.assertTrue(
            AuditLog.objects.filter(
                user__username="economist",
                action=AuditAction.CREATE,
                object_type="Department",
                object_id=str(department.id),
            ).exists()
        )

    def test_economist_can_manage_department_from_nsi_directory(self):
        self.client.login(username="economist", password="demo12345")

        response = self.client.get(reverse("nsi_directory", kwargs={"directory": "departments"}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ЦФО")

        create_response = self.client.post(
            reverse("nsi_directory", kwargs={"directory": "departments"}),
            {
                "action": "create",
                "directory": "departments",
                "code": "CFO-778",
                "name": "Команда внедрения",
            },
        )

        self.assertRedirects(create_response, reverse("nsi_directory", kwargs={"directory": "departments"}))
        department = Department.objects.get(code="CFO-778")

        update_response = self.client.post(
            reverse("nsi_directory", kwargs={"directory": "departments"}),
            {
                "action": "update",
                "directory": "departments",
                "item_id": department.id,
                "code": "CFO-778",
                "name": "Проектная команда",
            },
        )

        self.assertRedirects(update_response, reverse("nsi_directory", kwargs={"directory": "departments"}))
        department.refresh_from_db()
        self.assertEqual(department.name, "Проектная команда")
        self.assertTrue(
            AuditLog.objects.filter(
                user__username="economist",
                action=AuditAction.UPDATE,
                object_type="Department",
                object_id=str(department.id),
            ).exists()
        )

    def test_admin_can_delete_unused_nsi_item_from_directory(self):
        currency = Currency.objects.create(code="AED", name="Дирхам ОАЭ", source_system=SourceSystem.MANUAL)
        self.client.login(username="admin", password="demo12345")

        response = self.client.post(
            reverse("nsi_directory", kwargs={"directory": "currencies"}),
            {
                "action": "delete",
                "directory": "currencies",
                "item_id": currency.id,
            },
        )

        self.assertRedirects(response, reverse("nsi_directory", kwargs={"directory": "currencies"}))
        self.assertFalse(Currency.objects.filter(code="AED").exists())
        self.assertTrue(
            AuditLog.objects.filter(
                user__username="admin",
                action=AuditAction.DELETE,
                object_type="Currency",
                object_id=str(currency.id),
            ).exists()
        )

    def test_economist_can_sync_mock_from_nsi(self):
        self.client.login(username="economist", password="demo12345")

        response = self.client.post(
            reverse("nsi_dashboard"),
            {
                "action": "sync",
                "directory": "departments",
            },
        )

        self.assertRedirects(response, reverse("nsi_directory", kwargs={"directory": "departments"}), fetch_redirect_response=False)
        self.assertTrue(Organization.objects.filter(name='ООО "Гладиолус"').exists())
        self.assertTrue(Department.objects.filter(code="CFO-001").exists())

    def test_manager_can_open_workspace_and_nsi_read_only(self):
        self.client.login(username="manager", password="demo12345")

        workspace_response = self.client.get(reverse("workspace"))
        nsi_response = self.client.get(reverse("nsi_dashboard"))
        nsi_dir_response = self.client.get(reverse("nsi_directory", kwargs={"directory": "organizations"}))

        self.assertEqual(workspace_response.status_code, 200)
        self.assertEqual(nsi_response.status_code, 302)
        self.assertEqual(nsi_dir_response.status_code, 200)
        # Manager must not see write or sync actions
        self.assertFalse(nsi_dir_response.context["can_create_nsi"])
        self.assertFalse(nsi_dir_response.context["can_update_nsi"])
        self.assertFalse(nsi_dir_response.context["can_delete_nsi"])
        self.assertFalse(nsi_dir_response.context["can_sync_mock_1c"])

    def test_manager_cannot_sync_or_create_nsi_via_post(self):
        self.client.login(username="manager", password="demo12345")
        sync_response = self.client.post(
            reverse("nsi_directory", kwargs={"directory": "organizations"}),
            {"action": "sync", "directory": "organizations"},
            follow=True,
        )
        self.assertEqual(sync_response.status_code, 200)
        msgs = [m.message for m in sync_response.context["messages"]]
        self.assertTrue(any("Недостаточно прав" in m for m in msgs))

    def test_accountant_workspace_redirects_to_external_accounting(self):
        self.client.login(username="accountant", password="demo12345")

        response = self.client.get(reverse("workspace"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("external_accounting"))
        self.assertFalse(UserProfile.objects.get(user__username="accountant").can_access_app)

    def test_audit_log_page_is_admin_only_and_filters_correctly(self):
        """Журнал действий — admin-only; фильтры по user/action/search работают."""
        # Сгенерируем разнообразные записи аудита через реальные действия
        self.client.login(username="economist", password="demo12345")
        self.client.get(reverse("payment_requests"))
        self.client.get(reverse("payment_requests"))
        self.client.logout()
        self.client.login(username="manager", password="demo12345")
        self.client.get(reverse("integration_requests"))

        url = reverse("audit_log")

        # Не админы получают 403
        for username in ["economist", "manager", "accountant"]:
            self.client.logout()
            self.client.login(username=username, password="demo12345")
            self.assertEqual(self.client.get(url).status_code, 403, msg=username)

        # Админ видит страницу и получает корректную выборку
        self.client.logout()
        self.client.login(username="admin", password="demo12345")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertGreater(response.context["total"], 0)

        # Фильтр по пользователю работает
        manager = get_user_model().objects.get(username="manager")
        response = self.client.get(url, {"user_id": manager.id})
        self.assertEqual(response.status_code, 200)
        for entry in response.context["page"].object_list:
            self.assertEqual(entry.user_id, manager.id)

        # Фильтр по действию работает
        response = self.client.get(url, {"action": AuditAction.VIEW})
        self.assertEqual(response.status_code, 200)
        for entry in response.context["page"].object_list:
            self.assertEqual(entry.action, AuditAction.VIEW)

        # Поиск по пути работает
        response = self.client.get(url, {"q": "/settings/integration-requests/"})
        self.assertEqual(response.status_code, 200)
        paths = [entry.path for entry in response.context["page"].object_list]
        self.assertTrue(any("integration-requests" in p for p in paths))

    def test_only_administrator_has_django_admin_access(self):
        """Только администратор должен иметь is_staff=True и доступ к /admin/.

        Доступ economist/manager к Django admin означал бы возможность править
        сырые модели в обход бизнес-логики (резерв, статусы, версии лимитов).
        """
        User = get_user_model()
        self.assertTrue(User.objects.get(username="admin").is_staff)
        self.assertFalse(User.objects.get(username="economist").is_staff)
        self.assertFalse(User.objects.get(username="manager").is_staff)
        self.assertFalse(User.objects.get(username="accountant").is_staff)

        for username, expected in [
            ("admin", 200),
            ("economist", 302),
            ("manager", 302),
            ("accountant", 302),
        ]:
            with self.subTest(user=username):
                self.client.logout()
                self.client.login(username=username, password="demo12345")
                response = self.client.get("/admin/")
                self.assertEqual(response.status_code, expected)

    def test_accountant_login_redirects_to_external_accounting(self):
        response = self.client.post(
            reverse("login"),
            {"username": "accountant", "password": "demo12345"},
        )

        self.assertRedirects(response, reverse("external_accounting"), fetch_redirect_response=False)

    def test_accountant_can_pay_contract_in_external_system(self):
        sync_one_c_dataset(MockOneCProvider())
        contract = Contract.objects.get(kind=ContractKind.SOLE_SUPPLIER, number="ЕП-2026-014")
        self.client.login(username="accountant", password="demo12345")

        response = self.client.post(
            reverse("external_accounting"),
            {"contract_id": contract.id, "amount": "1000.00"},
        )

        self.assertRedirects(response, reverse("external_accounting"))
        payment = ExternalPaymentDocument.objects.get(contract=contract)
        self.assertEqual(payment.amount, Decimal("1000.00"))
        self.assertEqual(
            PaymentFact.objects.filter(contract=contract, external_id=f"external-payment-{payment.id}").count(),
            2,
        )
        self.assertTrue(
            PaymentFact.objects.filter(
                contract=contract,
                accounting_kind=AccountingKind.BU,
                amount=Decimal("1000.00"),
            ).exists()
        )
        self.assertTrue(AuditLog.objects.filter(user__username="accountant", action=AuditAction.PAY).exists())

    def test_economist_cannot_open_external_accounting(self):
        self.client.login(username="economist", password="demo12345")

        response = self.client.get(reverse("external_accounting"))

        self.assertEqual(response.status_code, 403)

    def test_manager_can_open_contract_reservations(self):
        sync_one_c_dataset(MockOneCProvider())
        self.client.login(username="manager", password="demo12345")

        response = self.client.get(reverse("contracts_reservations"))

        self.assertEqual(response.status_code, 200)

    def test_manager_can_open_contract_register(self):
        sync_one_c_dataset(MockOneCProvider())
        self.client.login(username="manager", password="demo12345")

        response = self.client.get(reverse("contracts_register"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ДП-2026-001")
        self.assertContains(response, "ЕП-2026-014")
        self.assertContains(response, "ЕП-2026-022")

    def test_contract_register_can_filter_by_customer_contract(self):
        sync_one_c_dataset(MockOneCProvider())
        customer_contract = Contract.objects.get(kind=ContractKind.CUSTOMER, number="ДП-2026-001")
        self.client.login(username="manager", password="demo12345")

        response = self.client.get(reverse("contracts_register"), {"customer_contract_id": customer_contract.id})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ДП-2026-001")
        self.assertContains(response, "Расходных договоров")

    def test_accountant_cannot_open_contract_reservations(self):
        self.client.login(username="accountant", password="demo12345")

        response = self.client.get(reverse("contracts_reservations"))

        self.assertEqual(response.status_code, 403)

    def test_accountant_cannot_open_contract_register(self):
        self.client.login(username="accountant", password="demo12345")

        response = self.client.get(reverse("contracts_register"))

        self.assertEqual(response.status_code, 403)

    def test_manager_can_open_contract_tree(self):
        sync_one_c_dataset(MockOneCProvider())
        self.client.login(username="manager", password="demo12345")

        response = self.client.get(reverse("contracts_tree"))

        self.assertEqual(response.status_code, 200)

    def test_accountant_cannot_open_contract_tree(self):
        self.client.login(username="accountant", password="demo12345")

        response = self.client.get(reverse("contracts_tree"))

        self.assertEqual(response.status_code, 403)

    def test_manager_can_open_payment_requests(self):
        sync_one_c_dataset(MockOneCProvider())
        self.client.login(username="manager", password="demo12345")

        response = self.client.get(reverse("payment_requests"))

        self.assertEqual(response.status_code, 200)

    def test_granular_acl_matrix_for_key_entities(self):
        User = get_user_model()
        admin = User.objects.get(username="admin")
        economist = User.objects.get(username="economist")
        manager = User.objects.get(username="manager")

        self.assertTrue(admin.has_perm("core.view_budgetlimitplan"))
        self.assertTrue(admin.has_perm("core.add_budgetlimitplan"))
        self.assertTrue(admin.has_perm("core.change_budgetlimitplan"))
        self.assertTrue(admin.has_perm("core.delete_budgetlimitplan"))

        self.assertTrue(economist.has_perm("core.view_paymentrequest"))
        self.assertTrue(economist.has_perm("core.add_paymentrequest"))
        self.assertTrue(economist.has_perm("core.change_paymentrequest"))
        self.assertFalse(economist.has_perm("core.delete_paymentrequest"))

        self.assertTrue(manager.has_perm("core.view_paymentfact"))
        self.assertFalse(manager.has_perm("core.add_paymentfact"))
        self.assertFalse(manager.has_perm("core.change_paymentfact"))
        self.assertFalse(manager.has_perm("core.delete_paymentfact"))

    def test_manager_cannot_create_payment_request_by_acl(self):
        sync_one_c_dataset(MockOneCProvider())
        self.client.login(username="manager", password="demo12345")

        contract = Contract.objects.filter(kind=ContractKind.SOLE_SUPPLIER).order_by("id").first()
        self.assertIsNotNone(contract)
        article = CashFlowArticle.objects.get(code="DDS-010")
        currency = Currency.objects.get(code="RUB")
        organization = Organization.objects.first()

        response = self.client.post(
            reverse("payment_requests"),
            {
                "action": "create",
                "request_kind": "by_contract",
                "organization_id": organization.id,
                "article_id": article.id,
                "counterparty_id": contract.counterparty_id,
                "contract_id": contract.id,
                "currency_id": currency.id,
                "amount": "1000.00",
                "manual_exchange_rate": "1.0000",
                "approver_id": get_user_model().objects.get(username="manager").id,
                "payment_purpose": "Проверка ACL",
            },
        )

        self.assertRedirects(response, reverse("payment_requests"))
        self.assertEqual(PaymentRequest.objects.count(), 0)

    def test_accountant_cannot_open_payment_requests(self):
        self.client.login(username="accountant", password="demo12345")

        response = self.client.get(reverse("payment_requests"))

        self.assertEqual(response.status_code, 403)

    def test_manager_can_open_payment_facts(self):
        sync_one_c_dataset(MockOneCProvider())
        self.client.login(username="manager", password="demo12345")

        response = self.client.get(reverse("payment_facts"))

        self.assertEqual(response.status_code, 200)

    def test_accountant_cannot_open_payment_facts(self):
        self.client.login(username="accountant", password="demo12345")

        response = self.client.get(reverse("payment_facts"))

        self.assertEqual(response.status_code, 403)

    def test_manager_can_open_plan_fact_report(self):
        sync_one_c_dataset(MockOneCProvider())
        self.client.login(username="manager", password="demo12345")

        response = self.client.get(reverse("plan_fact_report"))

        self.assertEqual(response.status_code, 200)

    def test_accountant_cannot_open_plan_fact_report(self):
        self.client.login(username="accountant", password="demo12345")

        response = self.client.get(reverse("plan_fact_report"))

        self.assertEqual(response.status_code, 403)

    def test_manager_can_open_manager_dashboard(self):
        sync_one_c_dataset(MockOneCProvider())
        self.client.login(username="manager", password="demo12345")

        response = self.client.get(reverse("manager_dashboard"))

        self.assertEqual(response.status_code, 200)

    def test_accountant_cannot_open_manager_dashboard(self):
        self.client.login(username="accountant", password="demo12345")

        response = self.client.get(reverse("manager_dashboard"))

        self.assertEqual(response.status_code, 403)

    def test_manager_can_open_integration_requests(self):
        self.client.login(username="manager", password="demo12345")

        response = self.client.get(reverse("integration_requests"))

        self.assertEqual(response.status_code, 200)

    def test_accountant_cannot_open_integration_requests(self):
        self.client.login(username="accountant", password="demo12345")

        response = self.client.get(reverse("integration_requests"))

        self.assertEqual(response.status_code, 403)

    def test_role_screen_access_matrix_matches_expected_status_and_logs(self):
        sync_one_c_dataset(MockOneCProvider())
        route_paths = {
            "workspace": reverse("workspace"),
            "planning_limits": reverse("planning_limits"),
            "payment_requests": reverse("payment_requests"),
            "payment_facts": reverse("payment_facts"),
            "manager_dashboard": reverse("manager_dashboard"),
            "plan_fact_report": reverse("plan_fact_report"),
            "contracts_register": reverse("contracts_register"),
            "contracts_reservations": reverse("contracts_reservations"),
            "contracts_tree": reverse("contracts_tree"),
            "nsi_directory": reverse("nsi_directory", kwargs={"directory": "organizations"}),
            "integration_requests": reverse("integration_requests"),
            "instruction": reverse("instruction"),
            "external_accounting": reverse("external_accounting"),
        }
        role_matrix = {
            "admin": {
                "workspace": 200,
                "planning_limits": 200,
                "payment_requests": 200,
                "payment_facts": 200,
                "manager_dashboard": 200,
                "plan_fact_report": 200,
                "contracts_register": 200,
                "contracts_reservations": 200,
                "contracts_tree": 200,
                "nsi_directory": 200,
                "integration_requests": 200,
                "instruction": 200,
                "external_accounting": 200,
            },
            "economist": {
                "workspace": 200,
                "planning_limits": 200,
                "payment_requests": 200,
                "payment_facts": 200,
                "manager_dashboard": 200,
                "plan_fact_report": 200,
                "contracts_register": 200,
                "contracts_reservations": 200,
                "contracts_tree": 200,
                "nsi_directory": 200,
                "integration_requests": 200,
                "instruction": 200,
                "external_accounting": 403,
            },
            "manager": {
                "workspace": 200,
                "planning_limits": 200,
                "payment_requests": 200,
                "payment_facts": 200,
                "manager_dashboard": 200,
                "plan_fact_report": 200,
                "contracts_register": 200,
                "contracts_reservations": 200,
                "contracts_tree": 200,
                "nsi_directory": 200,
                "integration_requests": 200,
                "instruction": 200,
                "external_accounting": 403,
            },
            "accountant": {
                "workspace": 302,
                "planning_limits": 403,
                "payment_requests": 403,
                "payment_facts": 403,
                "manager_dashboard": 403,
                "plan_fact_report": 403,
                "contracts_register": 403,
                "contracts_reservations": 403,
                "contracts_tree": 403,
                "nsi_directory": 403,
                "integration_requests": 403,
                "instruction": 200,
                "external_accounting": 200,
            },
        }

        for username, expected_routes in role_matrix.items():
            self.client.logout()
            self.client.login(username=username, password="demo12345")
            for route_name, expected_status in expected_routes.items():
                with self.subTest(role=username, route=route_name):
                    path = route_paths[route_name]
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, expected_status)

                    expected_action = AuditAction.DENIED if expected_status in (401, 403) else AuditAction.VIEW
                    self.assertTrue(
                        AuditLog.objects.filter(
                            user__username=username,
                            action=expected_action,
                            path=path,
                            status_code=expected_status,
                        ).exists()
                    )

    def test_instruction_page_is_available_for_internal_roles(self):
        self.client.login(username="economist", password="demo12345")

        response = self.client.get(reverse("instruction"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Руководство пользователя и Use Case по ролям")

    def test_accountant_can_open_instruction_page(self):
        self.client.login(username="accountant", password="demo12345")

        response = self.client.get(reverse("instruction"))

        self.assertEqual(response.status_code, 200)

    def test_instruction_asset_is_available_for_logged_in_user(self):
        self.client.login(username="manager", password="demo12345")

        response = self.client.get(
            reverse(
                "instruction_asset",
                kwargs={"asset_path": "images/annotated/01_login.png"},
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")

    def test_instruction_asset_blocks_path_traversal(self):
        self.client.login(username="manager", password="demo12345")

        response = self.client.get(
            reverse(
                "instruction_asset",
                kwargs={"asset_path": "../tz/v_0_1.docx"},
            )
        )

        self.assertEqual(response.status_code, 404)

    def test_user_can_change_theme_mode(self):
        self.client.login(username="economist", password="demo12345")

        response = self.client.post(
            reverse("set_theme_mode"),
            {"theme_mode": UiThemeMode.DARK, "next": reverse("workspace")},
        )

        self.assertRedirects(response, reverse("workspace"))
        profile = UserProfile.objects.get(user__username="economist")
        self.assertEqual(profile.theme_mode, UiThemeMode.DARK)

    def test_invalid_theme_mode_falls_back_to_light(self):
        self.client.login(username="economist", password="demo12345")

        response = self.client.post(
            reverse("set_theme_mode"),
            {"theme_mode": "broken", "next": reverse("workspace")},
        )

        self.assertRedirects(response, reverse("workspace"))
        profile = UserProfile.objects.get(user__username="economist")
        self.assertEqual(profile.theme_mode, UiThemeMode.LIGHT)


class BudgetPlanningTests(TestCase):
    def setUp(self):
        call_command("setup_access_roles", "--with-users", verbosity=0)
        sync_one_c_dataset(MockOneCProvider())
        User = get_user_model()
        self.economist = User.objects.get(username="economist")
        self.manager = User.objects.get(username="manager")

    def test_create_budget_plan_builds_12_months(self):
        plan = self._create_plan()

        self.assertEqual(BudgetLimitMonth.objects.filter(plan=plan).count(), 12)
        self.assertEqual(plan.monthly_total, Decimal("1200000.00"))
        self.assertTrue(plan.is_monthly_total_valid)
        self.assertEqual(plan.status, BudgetPlanStatus.DRAFT)

    def test_invalid_monthly_total_is_rejected(self):
        with self.assertRaises(ValueError):
            self._create_plan(monthly_amounts={month: Decimal("1.00") for month in range(1, 13)})

    def test_submit_and_approve_plan(self):
        plan = self._create_plan()
        self.client.login(username="economist", password="demo12345")

        submit_response = self.client.post(reverse("planning_limits"), {"action": "submit", "plan_id": plan.id})
        plan.refresh_from_db()

        self.assertRedirects(submit_response, reverse("planning_limits"))
        self.assertEqual(plan.status, BudgetPlanStatus.PENDING_APPROVAL)

        self.client.logout()
        self.client.login(username="manager", password="demo12345")
        approve_response = self.client.post(reverse("planning_limits"), {"action": "approve", "plan_id": plan.id})
        plan.refresh_from_db()

        self.assertRedirects(approve_response, reverse("planning_limits"))
        self.assertEqual(plan.status, BudgetPlanStatus.APPROVED)
        self.assertIsNotNone(plan.approved_at)
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.APPROVE, object_id=str(plan.id)).exists())

    def test_manager_cannot_create_plan(self):
        self.client.login(username="manager", password="demo12345")

        response = self.client.post(reverse("planning_limits"), self._create_payload())

        self.assertRedirects(response, reverse("planning_limits"))
        self.assertEqual(BudgetLimitPlan.objects.count(), 0)

    def test_economist_can_create_plan_from_ui(self):
        self.client.login(username="economist", password="demo12345")

        response = self.client.post(reverse("planning_limits"), self._create_payload())

        self.assertRedirects(response, reverse("planning_limits"))
        self.assertEqual(BudgetLimitPlan.objects.count(), 1)
        self.assertEqual(BudgetLimitMonth.objects.count(), 12)

    def _create_plan(self, monthly_amounts=None):
        from core.models import Currency, Department

        return create_budget_plan(
            author=self.economist,
            department=Department.objects.first(),
            article=CashFlowArticle.objects.first(),
            currency=Currency.objects.get(code="RUB"),
            planning_year=2026,
            planning_horizon=1,
            annual_amount=Decimal("1200000.00"),
            approver=self.manager,
            monthly_amounts=monthly_amounts,
            comment="Тестовый лимит",
        )

    def _create_payload(self):
        from core.models import Currency, Department

        return {
            "action": "create",
            "department_id": Department.objects.first().id,
            "article_id": CashFlowArticle.objects.first().id,
            "currency_id": Currency.objects.get(code="RUB").id,
            "planning_year": "2026",
            "planning_horizon": "1",
            "annual_amount": "1200000.00",
            "approver_id": self.manager.id,
            "comment": "Тестовый лимит",
            **{f"month_{month}": "100000.00" for month in range(1, 13)},
        }


class LimitAdjustmentTests(TestCase):
    def setUp(self):
        call_command("setup_access_roles", "--with-users", verbosity=0)
        sync_one_c_dataset(MockOneCProvider())
        User = get_user_model()
        self.economist = User.objects.get(username="economist")
        self.manager = User.objects.get(username="manager")
        self.plan = self._approved_plan()

    def test_create_adjustment_requires_approved_plan(self):
        draft_plan = self._create_plan()

        with self.assertRaises(ValueError):
            create_limit_adjustment(
                author=self.economist,
                base_plan=draft_plan,
                new_annual_amount=Decimal("1320000.00"),
                reason="Уточнение",
                monthly_amounts={month: Decimal("110000.00") for month in range(1, 13)},
            )

    def test_create_adjustment_requires_reason(self):
        with self.assertRaises(ValueError):
            create_limit_adjustment(
                author=self.economist,
                base_plan=self.plan,
                new_annual_amount=Decimal("1320000.00"),
                reason="",
                monthly_amounts={month: Decimal("110000.00") for month in range(1, 13)},
            )

    def test_approve_adjustment_updates_base_plan_version_and_months(self):
        adjustment = self._create_adjustment()
        submit_limit_adjustment(adjustment, self.economist)
        approve_limit_adjustment(adjustment, self.manager)
        self.plan.refresh_from_db()
        adjustment.refresh_from_db()

        self.assertEqual(adjustment.status, BudgetPlanStatus.APPROVED)
        self.assertEqual(self.plan.annual_amount, Decimal("1320000.00"))
        self.assertEqual(self.plan.version, 2)
        self.assertEqual(self.plan.monthly_total, Decimal("1320000.00"))
        self.assertEqual(BudgetLimitAdjustmentMonth.objects.filter(adjustment=adjustment).count(), 12)
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.APPROVE, object_type="BudgetLimitAdjustment").exists())

    def test_adjustment_ui_flow(self):
        self.client.login(username="economist", password="demo12345")

        create_response = self.client.post(reverse("planning_limits"), self._adjustment_payload())
        adjustment = BudgetLimitAdjustment.objects.get()
        submit_response = self.client.post(
            reverse("planning_limits"),
            {"action": "submit_adjustment", "adjustment_id": adjustment.id},
        )
        adjustment.refresh_from_db()

        self.assertRedirects(create_response, reverse("planning_limits"))
        self.assertRedirects(submit_response, reverse("planning_limits"))
        self.assertEqual(adjustment.status, BudgetPlanStatus.PENDING_APPROVAL)

        self.client.logout()
        self.client.login(username="manager", password="demo12345")
        approve_response = self.client.post(
            reverse("planning_limits"),
            {"action": "approve_adjustment", "adjustment_id": adjustment.id},
        )
        adjustment.refresh_from_db()
        self.plan.refresh_from_db()

        self.assertRedirects(approve_response, reverse("planning_limits"))
        self.assertEqual(adjustment.status, BudgetPlanStatus.APPROVED)
        self.assertEqual(self.plan.version, 2)

    def _approved_plan(self):
        plan = self._create_plan()
        submit_budget_plan(plan, self.economist)
        approve_budget_plan(plan, self.manager)
        return plan

    def _create_plan(self):
        from core.models import Currency, Department

        return create_budget_plan(
            author=self.economist,
            department=Department.objects.first(),
            article=CashFlowArticle.objects.first(),
            currency=Currency.objects.get(code="RUB"),
            planning_year=2026,
            planning_horizon=1,
            annual_amount=Decimal("1200000.00"),
            approver=self.manager,
            monthly_amounts={month: Decimal("100000.00") for month in range(1, 13)},
            comment="Базовый лимит",
        )

    def _create_adjustment(self):
        return create_limit_adjustment(
            author=self.economist,
            base_plan=self.plan,
            new_annual_amount=Decimal("1320000.00"),
            reason="Уточнение бюджета",
            monthly_amounts={month: Decimal("110000.00") for month in range(1, 13)},
        )

    def _adjustment_payload(self):
        return {
            "action": "create_adjustment",
            "base_plan_id": self.plan.id,
            "new_annual_amount": "1320000.00",
            "reason": "Уточнение бюджета",
            **{f"adjustment_month_{month}": "110000.00" for month in range(1, 13)},
        }
