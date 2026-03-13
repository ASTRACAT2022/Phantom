# Phantom Control Plane

Админ-панель для VPN/proxy вокруг `FPTN` с shadcn-подобным интерфейсом и рабочим backend-функционалом:

- управление пользователями: создание, удаление, заморозка, продление подписок;
- генерация и сброс FPTN access keys;
- мониторинг активных IP, сессий, трафика и edge-узлов;
- синхронизация конфигов в совместимые файлы `FPTN`.
- поддержка lightweight node-controller для FPTN-ноды с heartbeat в панель.

Полное руководство:

- [MANUAL.md](/Users/astracat/Documents/Phantom/MANUAL.md)

## Что умеет

- пишет `users.list` в формате `FPTN`;
- собирает `servers.json`, `premium_servers.json`, `servers_censored_zone.json`;
- генерирует `.fptn`-конфиг и `fptn:` access link;
- поддерживает дату истечения подписки и автоматическое отключение истёкших пользователей;
- умеет читать live-метрики FPTN из `FPTN_PROMETHEUS_METRICS_URL`, если вы укажете URL вида:
  - `https://your-fptn-host/api/v1/metrics/<secret>`

## Запуск

```bash
python3 -m pip install -r requirements.txt
uvicorn app.main:app --reload
```

Открыть:

- [http://127.0.0.1:8000](http://127.0.0.1:8000)

## Переменные окружения

```bash
APP_NAME="Phantom Control Plane"
DATABASE_URL=""
DATABASE_PATH="./data/panel.db"
FPTN_CONFIG_DIR="./fptn-config"
FPTN_SERVICE_NAME="PHANTOM.NET"
FPTN_PROMETHEUS_METRICS_URL=""
NODE_CONTROLLER_SHARED_TOKEN="phantom-node-shared-token"
BILLING_API_TOKEN="phantom-billing-token"
ADMIN_USERNAME="admin"
ADMIN_PASSWORD="admin-change-me"
SESSION_COOKIE_SECURE="false"
NODE_AGENT_GRPC_ENABLED="false"
NODE_AGENT_GRPC_HOST="0.0.0.0"
NODE_AGENT_GRPC_PORT="50061"
PHANTOM_SEED_DEMO="true"
PANEL_TIMEZONE="Europe/Moscow"
PANEL_HOST="0.0.0.0"
PANEL_PORT="8000"
```

Для production теперь рекомендуется `PostgreSQL`:

```bash
DATABASE_URL="postgresql://phantom:strongpass@127.0.0.1:5432/phantom"
```

Если `DATABASE_URL` не задан, панель продолжит работать на `SQLite`.

HTML-панель и `/docs` теперь закрыты admin-auth. После deploy вход выполняется через `/login`.
Если панель стоит за HTTPS reverse proxy, включи `SESSION_COOKIE_SECURE=true`.

Для связи панели и node-controller можно включить отдельный gRPC listener на любом свободном порту, например `51173`:

```bash
NODE_AGENT_GRPC_ENABLED="true"
NODE_AGENT_GRPC_PORT="51173"
```

## Что появится в `FPTN_CONFIG_DIR`

- `users.list`
- `servers.json`
- `premium_servers.json`
- `servers_censored_zone.json`
- `service_name.txt`

## Примечание по интеграции

Панель уже синхронизирует пользователей и server lists в формате `FPTN`. Если захотите, следующим шагом можно вынести её в отдельный API-слой, добавить авторизацию админов, Prometheus polling history и прямую работу с несколькими FPTN master/server-инстансами.

## Node Controller

Для подключения Linux-ноды с FPTN используйте агент из каталога [node-controller/README.md](/Users/astracat/Documents/Phantom/node-controller/README.md). Он шлёт в панель:

- uptime;
- load average;
- CPU / memory / disk;
- throughput интерфейса;
- текущее число соединений;
- `fptn_active_sessions`, если доступен локальный metrics endpoint.

One-line установка прямо на ноде:

```bash
curl -fsSL https://raw.githubusercontent.com/ASTRACAT2022/Phantom/main/node-controller/install-via-github.sh | \
sudo bash -s -- \
  --panel-url http://203.0.113.10:8000 \
  --shared-token phantom-node-shared-token \
  --node-name "Edge AMS-01" \
  --node-host 198.51.100.10 \
  --region Amsterdam
```

Если хочешь, чтобы ноды общались с панелью по gRPC, а не по HTTP:

```bash
curl -fsSL https://raw.githubusercontent.com/ASTRACAT2022/Phantom/main/node-controller/install-via-github.sh | \
sudo bash -s -- \
  --panel-url http://203.0.113.10:8000 \
  --shared-token phantom-node-shared-token \
  --transport grpc \
  --grpc-target 203.0.113.10:51173 \
  --node-name "Edge AMS-01" \
  --node-host 198.51.100.10 \
  --node-port 8443 \
  --region Amsterdam
```

One-shot деплой ноды:

```bash
cd /Users/astracat/Documents/Phantom/node-controller
sudo bash auto-deploy.sh \
  --panel-url http://203.0.113.10:8000 \
  --shared-token phantom-node-shared-token \
  --node-name "Edge AMS-01" \
  --node-host 198.51.100.10 \
  --region Amsterdam
```

Панель тоже может работать без домена и без SSL, например на `http://IP:8000`. Порт FPTN-ноды теперь можно задать в блоке `Node Defaults` внутри панели, и node-controller будет подхватывать его автоматически, если локально он не указан.

Если нода была зарегистрирована с неправильным `host`, можно пере-поднять её одной командой:

```bash
curl -fsSL https://raw.githubusercontent.com/ASTRACAT2022/Phantom/main/node-controller/install-via-github.sh | \
sudo bash -s -- \
  --panel-url http://203.0.113.10:8000 \
  --shared-token phantom-node-shared-token \
  --replace-existing \
  --node-name "Edge AMS-01" \
  --node-host 198.51.100.10 \
  --node-port 8443 \
  --region Amsterdam
```

`--replace-existing` удаляет старую запись ноды из панели по `agent_id`, пересобирает FPTN-конфиги и сразу регистрирует ноду заново. Адреса `198.51.100.10` и `203.0.113.10` в примерах тестовые: их нужно заменить на реальные IP.

## Production Deploy

Для production есть one-shot деплой панели через `systemd`.

Самый простой вариант:

```bash
cd /Users/astracat/Documents/Phantom
sudo bash easy-deploy.sh
```

Если нужен другой порт:

```bash
cd /Users/astracat/Documents/Phantom
sudo bash easy-deploy.sh --port 8080
```

Если нужен production сразу на `PostgreSQL`:

```bash
cd /Users/astracat/Documents/Phantom
sudo bash easy-deploy.sh \
  --database-url postgresql://phantom:strongpass@127.0.0.1:5432/phantom
```

Если нужен сразу random/high port для gRPC нод:

```bash
cd /Users/astracat/Documents/Phantom
sudo bash easy-deploy.sh --enable-node-grpc --grpc-port random
```

Можно сразу передать admin-логин:

```bash
sudo bash easy-deploy.sh \
  --admin-username admin \
  --admin-password strong-admin-password
```

Этот скрипт сам вызывает production deploy, подсказывает адрес панели и показывает, где смотреть сервис.

На Linux-сервере:

```bash
cd /Users/astracat/Documents/Phantom
sudo bash deploy/panel-auto-deploy.sh \
  --database-url postgresql://phantom:strongpass@127.0.0.1:5432/phantom \
  --panel-host 0.0.0.0 \
  --panel-port 8000 \
  --enable-node-grpc \
  --grpc-port random \
  --node-token phantom-node-shared-token \
  --billing-token phantom-billing-token
```

Скрипт сам:

- копирует проект в `/opt/phantom-control-plane`;
- создаёт venv и ставит зависимости;
- создаёт `/etc/phantom-control-plane.env`;
- ставит `systemd` unit;
- включает автозапуск и поднимает сервис;
- выводит URL панели и токены для node-controller и billing API.

Если указан `DATABASE_URL`, панель будет использовать `PostgreSQL`. Если нет, будет использоваться `SQLite`.
После deploy панель выводит admin username/password для первого входа.
Если включён `--enable-node-grpc`, deploy также выведет отдельный gRPC port для node-controller.

Для совсем простого запуска используй [easy-deploy.sh](/Users/astracat/Documents/Phantom/easy-deploy.sh), а если нужен полный контроль над путями и env, используй [deploy/panel-auto-deploy.sh](/Users/astracat/Documents/Phantom/deploy/panel-auto-deploy.sh).

После деплоя:

```bash
systemctl status phantom-control-plane.service
journalctl -u phantom-control-plane.service -f
```

Шаблон env-файла лежит в [deploy/panel.env.example](/Users/astracat/Documents/Phantom/deploy/panel.env.example), unit-файл в [deploy/phantom-control-plane.service](/Users/astracat/Documents/Phantom/deploy/phantom-control-plane.service), а сам one-shot скрипт в [deploy/panel-auto-deploy.sh](/Users/astracat/Documents/Phantom/deploy/panel-auto-deploy.sh).

## Backups

Теперь в production deploy входит система бэкапов:

- ручной backup: [deploy/backup.sh](/Users/astracat/Documents/Phantom/deploy/backup.sh)
- restore: [deploy/restore.sh](/Users/astracat/Documents/Phantom/deploy/restore.sh)
- daily timer: [deploy/phantom-backup.timer](/Users/astracat/Documents/Phantom/deploy/phantom-backup.timer)

Сделать backup вручную:

```bash
sudo bash /opt/phantom-control-plane/deploy/backup.sh
```

Восстановить backup:

```bash
sudo bash /opt/phantom-control-plane/deploy/restore.sh \
  --archive /var/backups/phantom-control-plane/phantom-backup-YYYYMMDD-HHMMSS.tar.gz
```

Проверить автозапуск:

```bash
systemctl status phantom-backup.timer
systemctl list-timers | grep phantom-backup
```

Если панель работает через `PostgreSQL`, backup использует `pg_dump`, а restore использует `pg_restore`.

## Billing API

Для интеграции с биллингом панель теперь умеет отдельный JSON API с bearer-токеном `BILLING_API_TOKEN`. Документация также доступна через FastAPI Swagger:

- `http://IP:PORT/docs`
  Документация теперь тоже требует admin login через cookie-сессию.

Основные endpoints:

- `GET /api/v1/billing/users/{username}`: получить полные данные пользователя, подписки и access key.
- `GET /api/v1/billing/subscriptions/check?username=...`: проверить статус подписки.
- `POST /api/v1/billing/users/upsert`: создать или обновить подписку и пользователя из биллинга.
- `POST /api/v1/billing/subscriptions/extend`: продлить подписку на `N` дней.
- `POST /api/v1/billing/subscriptions/status`: активировать, заморозить или отключить пользователя.
- `POST /api/v1/billing/subscriptions/speed`: выдать `full speed` или ограничить Mbps.
- `GET /api/v1/billing/access-keys/{username}`: забрать текущий ключ доступа.
- `POST /api/v1/billing/access-keys/rotate`: перевыпустить ключ доступа.

Примеры:

```bash
curl -H "Authorization: Bearer phantom-billing-token" \
  http://127.0.0.1:8000/api/v1/billing/subscriptions/check?username=client01
```

```bash
curl -X POST http://127.0.0.1:8000/api/v1/billing/users/upsert \
  -H "Authorization: Bearer phantom-billing-token" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "newcustomer",
    "plan_name": "pro-100",
    "billing_customer_id": "cus_001",
    "billing_subscription_id": "sub_001",
    "bandwidth_mbps": 100,
    "speed_mode": "limited",
    "subscription_days": 30,
    "is_premium": true,
    "status": "active"
  }'
```

```bash
curl -X POST http://127.0.0.1:8000/api/v1/billing/subscriptions/speed \
  -H "Authorization: Bearer phantom-billing-token" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "newcustomer",
    "speed_mode": "unlimited"
  }'
```

API возвращает в ответе нормализованные данные пользователя: статус, срок подписки, effective speed, premium flag, billing IDs и готовый access key для выдачи клиенту.
