# Phantom Node Controller

Лёгкий agent для Linux-ноды с `FPTN`, который сам подключается к админ-панели и шлёт heartbeat:

- uptime;
- load average;
- CPU / memory / disk;
- текущий сетевой трафик по интерфейсу;
- число соединений к ноде;
- `fptn_active_sessions`, если доступен локальный metrics endpoint.
- синхронизирует `users.list`, `servers.json`, `premium_servers.json`, `servers_censored_zone.json` и `service_name.txt` из панели в локальный `FPTN_CONFIG_DIR`.

По умолчанию агент шлёт heartbeat раз в `30` секунд, а FPTN-конфиг (`users.list` и server lists) подтягивает раз в `5` секунд. Это позволяет быстро доносить добавление/удаление пользователей до ноды без лишней нагрузки на heartbeat API.

## Быстрый деплой

На самой FPTN-ноде:

```bash
cd node-controller
sudo bash install.sh
sudo nano /etc/phantom-node-controller.env
sudo systemctl restart phantom-node-controller.service
sudo journalctl -u phantom-node-controller.service -f
```

## One-line install через GitHub

Если хочешь ставить ноду вообще без `git clone`, прямо одной командой:

```bash
curl -fsSL https://raw.githubusercontent.com/ASTRACAT2022/Phantom/main/node-controller/install-via-github.sh | \
sudo bash -s -- \
  --panel-url http://203.0.113.10:8000 \
  --shared-token phantom-node-shared-token \
  --node-name "Edge AMS-01" \
  --node-host 198.51.100.10 \
  --region Amsterdam
```

Если нужен другой порт FPTN:

```bash
curl -fsSL https://raw.githubusercontent.com/ASTRACAT2022/Phantom/main/node-controller/install-via-github.sh | \
sudo bash -s -- \
  --panel-url http://203.0.113.10:8000 \
  --shared-token phantom-node-shared-token \
  --node-name "Edge AMS-01" \
  --node-host 198.51.100.10 \
  --node-port 9443 \
  --region Amsterdam
```

Этот bootstrap-скрипт сам скачает `agent.py`, `phantom-node-controller.service` и `auto-deploy.sh` из GitHub, после чего поставит сервис.

## Полный прод-деплой FPTN + node-controller

Если хочешь не только agent, а сразу полноценную ноду с поднятием `FPTN`, используй full-stack installer:

```bash
curl -fsSL https://raw.githubusercontent.com/ASTRACAT2022/Phantom/main/node-controller/install-fptn-node.sh | \
sudo bash -s -- \
  --panel-url http://203.0.113.10:8000 \
  --shared-token phantom-node-shared-token \
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

Этот сценарий:

- ставит Docker и openssl на apt-based Linux;
- поднимает `FPTN` через Docker;
- поднимает локальный `fptn-proxy-server` для Prometheus-метрик;
- применяет throughput-oriented sysctl tuning (`bbr`, `fq`, socket buffers, backlog);
- генерирует `server.crt` и `server.key`;
- настраивает upstream DNS для `FPTN`;
- открывает порт в `ufw`, если он активен;
- ставит `node-controller` и сразу подключает ноду к панели.
- валидирует `8443`, metrics endpoint, heartbeat в панель и `users.list` before success.

Если DNS явно не передавать, full-stack installer по умолчанию использует `77.239.113.0` и `108.165.164.201`.

## gRPC transport

По умолчанию агент ходит в панель по HTTP. Если хочешь вынести heartbeat/config/deregister в отдельный gRPC listener панели на произвольном порту, можно установить ноду так:

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

Если host у gRPC тот же, что и у панели, можно не писать `--grpc-target`, а передать только отдельный порт:

```bash
sudo bash auto-deploy.sh \
  --panel-url http://203.0.113.10:8000 \
  --shared-token phantom-node-shared-token \
  --transport grpc \
  --grpc-port 51173 \
  --node-name "Edge AMS-01" \
  --node-host 198.51.100.10
```

При `--transport grpc` agent использует gRPC для:

- получения node defaults;
- heartbeat;
- deregister/re-register.

## One-shot автодеплой

Если хочешь один запуск без ручного редактирования env-файла:

```bash
cd node-controller
sudo bash auto-deploy.sh \
  --panel-url http://203.0.113.10:8000 \
  --shared-token phantom-node-shared-token \
  --node-name "Edge AMS-01" \
  --node-host 198.51.100.10 \
  --region Amsterdam
```

Скрипт сам:

- копирует agent в `/opt/phantom-node-controller`;
- создаёт `/etc/phantom-node-controller.env`;
- ставит `systemd` unit;
- делает `daemon-reload`;
- включает сервис в автозапуск;
- прогоняет post-deploy heartbeat и локальный self-check;
- сразу запускает сервис.

Если не передавать `--agent-id`, `--node-name`, `--node-host` или `--interface`, скрипт постарается определить их автоматически.
Если не передавать `--node-port`, `--tier` или `--region`, агент попробует взять defaults из панели.

## Пере-регистрация ноды

Если нода была зарегистрирована с неправильным IP/host, можно снять старую запись и сразу поднять новую той же командой:

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

Что делает `--replace-existing`:

- удаляет старую запись ноды из панели по `agent_id`;
- пересобирает FPTN server lists;
- заново регистрирует ноду с новым `host/port`.

По умолчанию для удаления используется текущий `agent_id`. Если нужно удалить запись с другим `agent_id`, передай `--replace-agent-id OLD_ID`.

## Без домена и SSL

Для самой админ-панели домен и SSL не обязательны. Node-controller спокойно работает с адресом вида:

```bash
http://SERVER_IP:8000
```

Важно: для `curl` нужно использовать именно `raw.githubusercontent.com`, а не обычную HTML-ссылку `github.com/...`, иначе скачается страница GitHub, а не shell-скрипт.
Адреса вида `198.51.100.10` и `203.0.113.10` в примерах выше тестовые; вместо них нужно подставлять реальный IP панели и ноды.

А FPTN-нода может слушать любой свободный порт, например `8443`, `9443` или `10443`. Этот порт можно:

- задать локально в env/автодеплое;
- или сохранить в `Node Defaults` внутри панели, и тогда агент подхватит его автоматически.

## Что нужно на панели

На панели должен совпадать shared token:

```bash
export NODE_CONTROLLER_SHARED_TOKEN="phantom-node-shared-token"
uvicorn app.main:app --reload
```

## Ручная проверка heartbeat

```bash
python3 agent.py --once
```

## Примечания

- Агент рассчитан на Linux-серверы с `/proc`.
- Для точного `md5_fingerprint` используется `openssl` и `FPTN_CERT_PATH`.
- Если `LOCAL_FPTN_METRICS_URL` не задан, `fptn_active_sessions` будет `0`, а панель всё равно увидит текущее число TCP-соединений к порту ноды.
