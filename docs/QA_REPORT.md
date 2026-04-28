# QA Report (Final)

Дата прогона: 28 апреля 2026  
Окружение: Docker Compose (`web` + `db`), локальный стенд `http://localhost:8000`

## 1) Автоматические проверки

- `python manage.py check` -> OK
- `python manage.py test core` -> OK (74/74)

## 2) Финальный UI smoke + визуальный аудит

Итоговый успешный прогон:

- Артефакты: `docs/user-guide/images/raw/ui_audit_final_release_rerun_20260428_063826/`
- Отчет: `docs/user-guide/images/raw/ui_audit_final_release_rerun_20260428_063826/smoke_report.txt`
- Проверено страниц: 40
- Выполнено шагов: 44
- Ошибок: 0

В каталоге прогона сохранены:

- скриншоты по ролям (`admin_*`, `economist_*`, `manager_*`, `accountant_*`)
- скриншоты функциональных действий:
  - `economist_action_payment_request_created.png`
  - `admin_action_integration_request_created.png`
  - `accountant_action_payment_posted.png`
  - `economist_action_ui_toggles.png`

## 3) Проверка ключевого функционала

Проверены и пройдены:

- рабочие экраны по ролям (планирование, заявки, факт, отчеты, НСИ, договоры, инструкция, настройки);
- создание заявки на оплату экономистом;
- создание заявки на интеграцию администратором;
- проведение оплаты в внешнем контуре бухгалтером;
- переключение темы и плотности таблиц.

## 4) ACL / доступы (ожидаемое поведение)

Проверены ограниченные доступы:

- `economist -> /external/accounting/` = 403 (ожидаемо)
- `manager -> /nsi/` = 403 (ожидаемо)
- `manager -> /external/accounting/` = 403 (ожидаемо)
- `accountant -> /` = 403 (ожидаемо)

Нарушений ролевой модели не выявлено.

## 5) Вывод

Приложение готово к демонстрации текущей итерации: серверные проверки, UI smoke, ключевые сценарии и ролевые ограничения прошли успешно.

