# Phantom Manual

Подробное руководство по панели `Phantom Control Plane` для `FPTN`.

## 1. Что это такое

`Phantom` это админ-панель для управления VPN/proxy-инфраструктурой вокруг `FPTN`.

Она решает 4 основные задачи:

- управление пользователями и подписками;
- генерация и перевыпуск ключей доступа;
- мониторинг узлов, сессий, трафика и активных IP;
- интеграция с биллингом через JSON API.

## 2. Состав проекта

- [app/main.py](/Users/astracat/Documents/Phantom/app/main.py) - FastAPI routes, HTML pages, action handlers и API.
- [app/service.py](/Users/astracat/Documents/Phantom/app/service.py) - ядро бизнес-логики панели.
- [app/fptn.py](/Users/astracat/Documents/Phantom/app/fptn.py) - генерация `FPTN`-совместимых конфигов и access keys.
- [templates/](/Users/astracat/Documents/Phantom/templates) - HTML шаблоны панели.
- [static/](/Users/astracat/Documents/Phantom/static) - стили и статика.
- [node-controller/](/Users/astracat/Documents/Phantom/node-controller) - агент для Linux-ноды.
- [deploy/](/Users/astracat/Documents/Phantom/deploy) - production deploy панели через `systemd`.
- [easy-deploy.sh](/Users/astracat/Documents/Phantom/easy-deploy.sh) - самый простой запуск production deploy.

## 3. Что умеет панель

- создавать пользователя;
- удалять пользователя;
- блокировать, размораживать и автоматически отключать истёкшие подписки;
- задавать скорость в Mbps;
- включать `full speed`;
- выдавать `.fptn`-конфиг и `fptn:` access link;
- перевыпускать ключ доступа;
- показывать активные сессии и IP;
- показывать трафик пользователей;
- показывать состояние нод;
- принимать heartbeat от node-controller;
- синхронизировать `users.list`, `servers.json`, `premium_servers.json`, `servers_censored_zone.json`;
- отдавать API для интеграции с биллингом.

## 4. Системные требования

Минимально:

- Linux сервер;
- `python3` 3.10+;
- `systemd` для production deploy;
- доступ к файловой системе для записи БД и `FPTN`-конфигов.

Для node-controller:

- Linux с `/proc`;
- `systemd`;
- желательно `openssl`, если нужен fingerprint по сертификату.

## 5. Быстрый локальный запуск

```bash
cd /Users/astracat/Documents/Phantom
python3 -m pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Открыть:

- [http://127.0.0.1:8000](http://127.0.0.1:8000)
- [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

## 6. Структура интерфейса

Основные разделы:

- `/dashboard` - сводка по инфраструктуре;
- `/users` - пользователи, подписки, ключи, скорость;
- `/sessions` - активные IP и сессии;
- `/nodes` - ноды и heartbeat;
- `/settings` - настройки панели, speed policy и node defaults.

## 7. Переменные окружения

Основные:

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
PANEL_PUBLIC_BASE_URL=""
FORWARDED_ALLOW_IPS="127.0.0.1"
PHANTOM_SEED_DEMO="false"
PANEL_TIMEZONE="Europe/Moscow"
PANEL_HOST="0.0.0.0"
PANEL_PORT="8000"
```

Что важно:

- `DATABASE_URL` - production-подключение к `PostgreSQL`;
- `DATABASE_PATH` - SQLite база панели;
- `FPTN_CONFIG_DIR` - куда панель пишет конфиги для `FPTN`;
- `NODE_CONTROLLER_SHARED_TOKEN` - токен для heartbeat от нод;
- `BILLING_API_TOKEN` - токен для внешнего биллинга;
- `ADMIN_USERNAME` - логин администратора панели;
- `ADMIN_PASSWORD` - пароль администратора панели;
- `SESSION_COOKIE_SECURE` - включить secure-cookie при работе за HTTPS;
- `PANEL_PUBLIC_BASE_URL` - внешний URL панели за reverse proxy, например `https://panel.example.com`;
- `FORWARDED_ALLOW_IPS` - список trusted proxy IP для `X-Forwarded-*`, по умолчанию `127.0.0.1`;
- `PHANTOM_SEED_DEMO=false` обязательно для production.

Рекомендуемый production вариант:

```bash
DATABASE_URL="postgresql://phantom:strongpass@127.0.0.1:5432/phantom"
```

Логика выбора БД:

- если `DATABASE_URL` задан и начинается с `postgresql://` или `postgres://`, используется `PostgreSQL`;
- если `DATABASE_URL` пустой, используется `SQLite`.

Доступ к панели:

- HTML-панель защищена admin login;
- `/docs` тоже защищён admin login;
- billing API и node-agent API используют свои bearer tokens отдельно.

## 8. Что пишет панель в FPTN

В каталог `FPTN_CONFIG_DIR`:

- `users.list`
- `servers.json`
- `premium_servers.json`
- `servers_censored_zone.json`
- `service_name.txt`

Это позволяет использовать панель как источник актуальных конфигов для `FPTN`.

## 9. Speed Policy

У пользователя есть 2 режима:

- `limited`
- `unlimited`

Логика:

- `limited` использует конкретное значение `bandwidth_mbps`;
- `unlimited` экспортируется в `FPTN` как высокий безопасный профиль;
- текущий production-safe лимит для `full speed` это `2047 Mbps`.

Настроить full speed profile можно в `/settings`.

## 10. Подписки

Для пользователя поддерживаются:

- `active`
- `suspended`
- `expired`

Поведение:

- `active` - пользователь доступен и попадает в экспорт `FPTN`;
- `suspended` - пользователь отключён вручную;
- `expired` - подписка истекла, пользователь автоматически отключён.

При истечении подписки:

- пользователь переводится в `expired`;
- активные сессии закрываются;
- пользователь исчезает из активного `users.list`.

## 11. Easy Deploy

Самый простой production запуск:

```bash
cd /Users/astracat/Documents/Phantom
sudo bash easy-deploy.sh
```

На другом порту:

```bash
sudo bash easy-deploy.sh --port 8080
```

Если сразу хочешь свои токены:

```bash
sudo bash easy-deploy.sh \
  --port 8080 \
  --node-token my-node-token \
  --billing-token my-billing-token
```

Если нужен production сразу на `PostgreSQL`:

```bash
sudo bash easy-deploy.sh \
  --database-url postgresql://phantom:strongpass@127.0.0.1:5432/phantom
```

Если нужно сразу задать admin user:

```bash
sudo bash easy-deploy.sh \
  --admin-username admin \
  --admin-password strong-admin-password
```

Скрипт:

- вызывает production deploy панели;
- ставит сервис `phantom-control-plane.service`;
- показывает URL панели;
- показывает URL Swagger;
- подсказывает команды для просмотра статуса.

## 12. Production Deploy

Если нужен более управляемый запуск:

```bash
cd /Users/astracat/Documents/Phantom
sudo bash deploy/panel-auto-deploy.sh \
  --database-url postgresql://phantom:strongpass@127.0.0.1:5432/phantom \
  --panel-host 0.0.0.0 \
  --panel-port 8000 \
  --node-token my-node-token \
  --billing-token my-billing-token
```

Что делает production deploy:

- создаёт пользователя `phantom`;
- копирует проект в `/opt/phantom-control-plane`;
- создаёт `venv`;
- ставит зависимости;
- создаёт `/etc/phantom-control-plane.env`;
- создаёт `/var/lib/phantom-control-plane`;
- ставит `systemd` unit;
- включает автозапуск сервиса.
- генерирует admin password, если он не был передан явно.

Если панель будет стоять за reverse proxy и слушать только localhost:

```bash
cd /Users/astracat/Documents/Phantom
sudo bash deploy/panel-auto-deploy.sh \
  --behind-proxy \
  --public-base-url https://panel.example.com \
  --panel-port 8000 \
  --node-token my-node-token \
  --billing-token my-billing-token
```

Этот режим автоматически:

- меняет bind на `127.0.0.1`;
- включает `SESSION_COOKIE_SECURE=true`;
- включает `uvicorn --proxy-headers`;
- доверяет `X-Forwarded-*` от `127.0.0.1`;
- заставляет UI использовать внешний URL прокси вместо локального `127.0.0.1`.

Важные файлы:

- [deploy/panel-auto-deploy.sh](/Users/astracat/Documents/Phantom/deploy/panel-auto-deploy.sh)
- [deploy/phantom-control-plane.service](/Users/astracat/Documents/Phantom/deploy/phantom-control-plane.service)
- [deploy/panel.env.example](/Users/astracat/Documents/Phantom/deploy/panel.env.example)

## 13. Работа без домена и SSL

Панель может работать просто на IP и кастомном порту:

- `http://SERVER_IP:8000`
- `http://SERVER_IP:8080`
- `http://SERVER_IP:9000`

Для внутренней админки это нормально. Если потом понадобится публичный production с HTTPS, можно поставить reverse proxy перед панелью.

Минимальный `Nginx` пример:

```nginx
server {
    listen 80;
    server_name panel.example.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

## 14. Node Controller

Агент нужен для того, чтобы панель видела:

- uptime ноды;
- load average;
- CPU;
- memory;
- disk usage;
- throughput;
- число текущих соединений;
- `fptn_active_sessions`, если доступен локальный metrics endpoint.

Документация агента:

- [node-controller/README.md](/Users/astracat/Documents/Phantom/node-controller/README.md)

One-shot установка на ноде:

```bash
cd /Users/astracat/Documents/Phantom/node-controller
sudo bash auto-deploy.sh \
  --panel-url http://SERVER_IP:8000 \
  --shared-token my-node-token \
  --node-name "Edge AMS-01" \
  --node-host 198.51.100.10 \
  --region Amsterdam
```

Полный прод-деплой новой ноды с `FPTN` и `node-controller`:

```bash
curl -fsSL https://raw.githubusercontent.com/ASTRACAT2022/Phantom/main/node-controller/install-fptn-node.sh | \
sudo bash -s -- \
  --panel-url http://SERVER_IP:8000 \
  --shared-token my-node-token \
  --node-name "Edge AMS-01" \
  --node-host 198.51.100.10 \
  --node-port 8443 \
  --region Amsterdam \
  --tier public \
  --proxy-domain vk.ru \
  --dns-ipv4-primary 77.239.113.0 \
  --dns-ipv4-secondary 108.165.164.201 \
  --open-ufw
```

Эти же команды теперь можно генерировать прямо в панели на странице `Nodes` через мастер `+ Новая прод-нода`.
Там же теперь можно задать upstream DNS для `FPTN`; по умолчанию используется `77.239.113.0` и `108.165.164.201`.
Для production live-metrics installer теперь автоматически поднимает `fptn-proxy-server` и направляет панель на него.

Если нужно удалить старую запись ноды и зарегистрировать её заново с новым IP:

```bash
curl -fsSL https://raw.githubusercontent.com/ASTRACAT2022/Phantom/main/node-controller/install-via-github.sh | \
sudo bash -s -- \
  --panel-url http://SERVER_IP:8000 \
  --shared-token my-node-token \
  --replace-existing \
  --node-name "Edge AMS-01" \
  --node-host 198.51.100.10 \
  --node-port 8443 \
  --region Amsterdam
```

## 15. Node Defaults

В панели можно задать дефолты для нод:

- host;
- port;
- tier;
- region;
- transport hint.

Если агент локально не получил часть параметров, он попробует взять их из панели.

Это удобно для массового подключения нод.

## 16. Billing API

Billing API закрыт bearer-токеном:

```bash
Authorization: Bearer BILLING_API_TOKEN
```

Документация в Swagger:

- `/docs`

Основные методы:

- `GET /api/v1/billing/users/{username}`
- `GET /api/v1/billing/subscriptions/check`
- `POST /api/v1/billing/users/upsert`
- `POST /api/v1/billing/subscriptions/extend`
- `POST /api/v1/billing/subscriptions/status`
- `POST /api/v1/billing/subscriptions/speed`
- `GET /api/v1/billing/access-keys/{username}`
- `POST /api/v1/billing/access-keys/rotate`

Поддерживаемые lookup-поля:

- `username`
- `billing_subscription_id`
- `billing_customer_id`

## 17. Примеры Billing API

Проверка подписки:

```bash
curl -H "Authorization: Bearer phantom-billing-token" \
  "http://127.0.0.1:8000/api/v1/billing/subscriptions/check?username=client01"
```

Создание или обновление пользователя:

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/billing/users/upsert" \
  -H "Authorization: Bearer phantom-billing-token" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "customer100",
    "plan_name": "pro-100",
    "billing_customer_id": "cus_100",
    "billing_subscription_id": "sub_100",
    "bandwidth_mbps": 100,
    "speed_mode": "limited",
    "subscription_days": 30,
    "is_premium": true,
    "status": "active"
  }'
```

Включить full speed:

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/billing/subscriptions/speed" \
  -H "Authorization: Bearer phantom-billing-token" \
  -H "Content-Type: application/json" \
  -d '{
    "billing_subscription_id": "sub_100",
    "speed_mode": "unlimited"
  }'
```

Отключить подписку:

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/billing/subscriptions/status" \
  -H "Authorization: Bearer phantom-billing-token" \
  -H "Content-Type: application/json" \
  -d '{
    "billing_subscription_id": "sub_100",
    "status": "suspended"
  }'
```

Получить ключ доступа:

```bash
curl -H "Authorization: Bearer phantom-billing-token" \
  "http://127.0.0.1:8000/api/v1/billing/access-keys/customer100"
```

## 18. Формат ответа billing API

API возвращает в нормализованном виде:

- `username`
- `status`
- `is_active`
- `plan_name`
- `is_premium`
- `bandwidth_mbps`
- `speed_mode`
- `effective_bandwidth_mbps`
- `subscription_expires_at`
- `billing_customer_id`
- `billing_subscription_id`
- `access_key`

Это удобно для CRM, Telegram-бота, checkout-сервиса и внешнего биллинга.

## 19. Безопасность

Минимальные рекомендации:

- поменять `NODE_CONTROLLER_SHARED_TOKEN`;
- поменять `BILLING_API_TOKEN`;
- выключить `PHANTOM_SEED_DEMO`;
- не открывать панель в интернет без ограничения доступа;
- если панель смотрит наружу, поставить reverse proxy и HTTPS;
- ограничить доступ по firewall;
- делать резервные копии БД и `FPTN_CONFIG_DIR`.

## 20. Резервное копирование

В production теперь есть встроенная backup-система.

Файлы:

- [deploy/backup.sh](/Users/astracat/Documents/Phantom/deploy/backup.sh)
- [deploy/restore.sh](/Users/astracat/Documents/Phantom/deploy/restore.sh)
- [deploy/phantom-backup.service](/Users/astracat/Documents/Phantom/deploy/phantom-backup.service)
- [deploy/phantom-backup.timer](/Users/astracat/Documents/Phantom/deploy/phantom-backup.timer)

Что попадает в backup:

- `SQLite` база или `PostgreSQL dump`;
- каталог `FPTN_CONFIG_DIR`;
- `/etc/phantom-control-plane.env`

Каталог backup по умолчанию:

```bash
/var/backups/phantom-control-plane
```

Ручной backup:

```bash
sudo bash /opt/phantom-control-plane/deploy/backup.sh
```

Поведение по типу БД:

- для `SQLite` используется safe-copy через SQLite backup API;
- для `PostgreSQL` используется `pg_dump`;
- для restore `PostgreSQL` используется `pg_restore`.

Автоматический backup:

- timer `phantom-backup.timer`
- сервис `phantom-backup.service`
- расписание по умолчанию: каждый день в `03:30`

Проверить таймер:

```bash
systemctl status phantom-backup.timer
systemctl list-timers | grep phantom-backup
```

Сделать restore:

```bash
sudo bash /opt/phantom-control-plane/deploy/restore.sh \
  --archive /var/backups/phantom-control-plane/phantom-backup-YYYYMMDD-HHMMSS.tar.gz
```

Если нужно восстановить ещё и env:

```bash
sudo bash /opt/phantom-control-plane/deploy/restore.sh \
  --archive /var/backups/phantom-control-plane/phantom-backup-YYYYMMDD-HHMMSS.tar.gz \
  --with-env
```

Параметры backup в env:

```bash
PHANTOM_BACKUP_DIR="/var/backups/phantom-control-plane"
PHANTOM_BACKUP_RETENTION_DAYS="14"
```

## 21. Обновление проекта

Базовый сценарий обновления:

1. Обновить код проекта.
2. Повторно запустить `easy-deploy.sh` или `deploy/panel-auto-deploy.sh`.
3. Проверить статус сервиса.
4. Проверить `/health`.
5. Проверить `/docs`.

Команды:

```bash
sudo bash easy-deploy.sh
systemctl status phantom-control-plane.service
curl http://127.0.0.1:8000/health
```

## 22. Полезные команды эксплуатации

Статус сервиса:

```bash
systemctl status phantom-control-plane.service
```

Логи:

```bash
journalctl -u phantom-control-plane.service -f
```

Проверка health:

```bash
curl http://127.0.0.1:8000/health
```

## 23. Частые проблемы

Панель не стартует:

- проверить `systemctl status phantom-control-plane.service`;
- посмотреть `journalctl -u phantom-control-plane.service -f`;
- проверить, что порт свободен.

Нода не видна:

- проверить `NODE_CONTROLLER_SHARED_TOKEN`;
- проверить, что node-controller реально запущен;
- проверить доступность панели с ноды;
- проверить firewall.

Нет точных live-метрик:

- проверить `FPTN_PROMETHEUS_METRICS_URL`;
- проверить доступ панели к endpoint метрик;
- убедиться, что endpoint отдаёт `fptn_active_sessions` и user traffic metrics.

Пользователь не попадает в FPTN:

- проверить статус пользователя;
- проверить дату подписки;
- выполнить ручную синхронизацию в `/settings`;
- проверить файлы в `FPTN_CONFIG_DIR`.

## 24. Что ещё можно сделать дальше

Следующие логичные этапы:

- авторизация админов;
- reverse proxy конфиг `Nginx`;
- Docker Compose;
- multi-server orchestration;
- webhooks под конкретный биллинг;
- Telegram-бот клиента;
- audit log действий админа.
