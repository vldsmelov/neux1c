# Quality and Production Baseline

Цель этого документа - зафиксировать минимальный стандарт качества для перехода от пилота к production-grade версии.

## Ориентиры качества

- ISO/IEC 25010: функциональная пригодность, надежность, безопасность, сопровождаемость, переносимость.
- OWASP ASVS: проверяемые требования к аутентификации, сессиям, загрузкам файлов, журналированию и конфигурации.
- WCAG 2.2 AA: доступность интерфейса для клавиатуры, screen reader, контраста и понятных подписей.
- Django deployment checklist: production-настройки безопасности и проверка `manage.py check --deploy`.

## Обязательные проверки

Локально:

```powershell
docker compose up --build
docker compose exec web python manage.py check
docker compose exec web python manage.py check --deploy
docker compose exec web python manage.py test core
```

В CI:

- `ruff check .`
- `pip-audit -r requirements.txt`
- `python manage.py check`
- `python manage.py check --deploy`
- `coverage run manage.py test core`
- `coverage report` с порогом 80%

## Production-настройки

Для production окружения должны быть явно заданы:

- `DJANGO_SECRET_KEY`
- `DJANGO_DEBUG=0`
- `DJANGO_ALLOWED_HOSTS`
- `DJANGO_CSRF_TRUSTED_ORIGINS`
- `DJANGO_SECURE_SSL_REDIRECT=1`
- `DJANGO_SESSION_COOKIE_SECURE=1`
- `DJANGO_CSRF_COOKIE_SECURE=1`
- `DJANGO_SECURE_HSTS_SECONDS=31536000`
- надежный `POSTGRES_PASSWORD`
- секретный `NE_UX_DEMO_PASSWORD` или отключение демо-пользователей

Dockerfile по умолчанию запускает `gunicorn`. `docker-compose.yml` остается dev-стендом с `runserver` и volume mount для быстрой разработки.

## Загрузка файлов

Файлы обоснований заявок проходят базовую серверную валидацию:

- максимальный размер: `NE_UX_MAX_JUSTIFICATION_FILE_SIZE`;
- whitelist расширений: `NE_UX_ALLOWED_JUSTIFICATION_EXTENSIONS`;
- whitelist MIME-типов: `NE_UX_ALLOWED_JUSTIFICATION_CONTENT_TYPES`;
- имя файла заменяется на UUID, чтобы не хранить пользовательское имя как путь.

Перед промышленной эксплуатацией нужно добавить антивирусную проверку и приватную выдачу файлов через авторизованный endpoint.

## Номера документов

Номера документов выдаются через таблицу `DocumentSequence` под транзакционной блокировкой. Это защищает от дублей при параллельном создании планов, заявок, оплат и заявок на интеграцию.

## Следующие шаги

- Перевести формы на Django `Form`/`ModelForm` с централизованной валидацией.
- Добавить Playwright smoke в CI и axe-проверки доступности.
- Вынести строки UI в `gettext_lazy` и подготовить `locale/`.
- Добавить structured logging, request id, метрики, алерты и runbook backup/restore.
