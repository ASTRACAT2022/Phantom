# Phantom Node Controller

Лёгкий agent для Linux-ноды с `FPTN`, который сам подключается к админ-панели и шлёт heartbeat:

- uptime;
- load average;
- CPU / memory / disk;
- текущий сетевой трафик по интерфейсу;
- число соединений к ноде;
- `fptn_active_sessions`, если доступен локальный metrics endpoint.

## Быстрый деплой

На самой FPTN-ноде:

```bash
cd node-controller
sudo bash install.sh
sudo nano /etc/phantom-node-controller.env
sudo systemctl restart phantom-node-controller.service
sudo journalctl -u phantom-node-controller.service -f
```

## One-shot автодеплой

Если хочешь один запуск без ручного редактирования env-файла:

```bash
cd node-controller
sudo bash auto-deploy.sh \
  --panel-url http://203.0.113.10:8000 \
  --shared-token phantom-node-shared-token \
  --node-name "Edge AMS-01" \
  --node-host 1.2.3.4 \
  --region Amsterdam
```

Скрипт сам:

- копирует agent в `/opt/phantom-node-controller`;
- создаёт `/etc/phantom-node-controller.env`;
- ставит `systemd` unit;
- делает `daemon-reload`;
- включает сервис в автозапуск;
- сразу запускает сервис.

Если не передавать `--agent-id`, `--node-name`, `--node-host` или `--interface`, скрипт постарается определить их автоматически.
Если не передавать `--node-port`, `--tier` или `--region`, агент попробует взять defaults из панели.

## Без домена и SSL

Для самой админ-панели домен и SSL не обязательны. Node-controller спокойно работает с адресом вида:

```bash
http://SERVER_IP:8000
```

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
