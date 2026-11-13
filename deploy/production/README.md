# Production deployment

Docker Compose stack для трёх публичных доменов:

| Домен | Сервис | Внутренний порт |
| --- | --- | --- |
| `FRONTEND_DOMAIN`, `www.FRONTEND_DOMAIN` | `xam_frontend` (React/Vite + nginx) | `80` |
| `BACKEND_DOMAIN` | `xam_backend` (FastAPI + PostgreSQL) | `8000` |
| `CALLS_DOMAIN` | `xam_call_service` (FastAPI + WebSocket) | `8081` |

TLS-сертификаты Let's Encrypt выпускает и обновляет Caddy. Наружу открыты
только `80/tcp`, `443/tcp`, `443/udp` (Caddy), `3478/tcp`, `3478/udp` и
`49160-49300/udp` (coturn), плюс SSH. PostgreSQL, backend и call service
порты наружу не публикуются.

## 1. DNS

Создайте A-записи на публичный IPv4 сервера:

| Имя | Тип | Значение |
| --- | --- | --- |
| `@` (`FRONTEND_DOMAIN`) | A | `SERVER_IP` |
| `www` | A | `SERVER_IP` |
| `backend` | A | `SERVER_IP` |
| `callservice` | A | `SERVER_IP` |

Если `www` не нужен, уберите его из `Caddyfile` и из `TLS_HOSTS` в
`docker-compose.prod.yml`.

## 2. Firewall

```text
22/tcp        SSH
80/tcp        Caddy / ACME HTTP challenge + redirect
443/tcp       Caddy HTTPS
443/udp       Caddy HTTP/3
3478/tcp      TURN
3478/udp      TURN / STUN
49160-49300/udp  TURN relay
```

Проверьте также firewall облачного провайдера (security group).

## 3. Подготовка сервера

Нужны Linux, Docker Engine, плагин `docker compose` и `git`:

```bash
docker --version
docker compose version
git --version
```

SSH-пользователь должен иметь доступ к Docker (обычно группа `docker`) и право
писать в каталог приложения. Каталог по умолчанию — `/opt/xam`; в нём будут три
репозитория:

```text
/opt/xam/xam_frontend
/opt/xam/xam_backend
/opt/xam/xam_call_service
```

Если репозитории приватные, настройте deploy key или доступ по токену для
пользователя, под которым выполняется деплой.

## 4. Настройка окружения

```bash
cd deploy/production
cp .env.production.example .env.production
```

Заполните `.env.production`:

- `SERVER_IP` — публичный IPv4 сервера. Пока IP неизвестен, оставьте
  `203.0.113.10`; после создания сервера замените на реальный.
- `TURN_EXTERNAL_IP` — обычно совпадает с `SERVER_IP`. Если сервер за NAT,
  укажите `PUBLIC_IP/PRIVATE_IP`.
- `ACME_EMAIL` — e-mail для Let's Encrypt.
- `SECRET_KEY` — тот же ключ, что в `xam_backend`. Сгенерируйте:
  `openssl rand -hex 32`.
- `POSTGRES_PASSWORD`, `TURN_CREDENTIAL` — длинные пароли. Используйте только
  буквы и цифры: пароль БД попадает в URL, а coturn раскрывает аргументы через
  `eval`.
- `FRONTEND_DOMAIN`, `BACKEND_DOMAIN`, `CALLS_DOMAIN` уже заданы.

`.env.production` не коммитится (`.gitignore`). STUN/TURN-адреса в compose
собираются из `SERVER_IP`, поэтому при смене IP достаточно поменять
`SERVER_IP` и `TURN_EXTERNAL_IP`.

## 5. Публикация с Windows

Настройте SSH-алиас в `%USERPROFILE%\.ssh\config`:

```sshconfig
Host <alias>
    HostName <server-ip>
    User <ssh-user>
    IdentityFile C:\Users\<you>\.ssh\id_ed25519
```

Затем из каталога `deploy/production`:

```powershell
.\publish.ps1
```

Скрипт упакует конфиги, загрузит их на сервер, обновит три репозитория,
пересоберёт образы и поднимет stack. Сервер разворачивает код из `origin/main`,
поэтому перед публикацией закоммитьте и запушьте изменения во всех трёх
репозиториях. Незакоммиченные и неотправленные изменения не попадут на сервер
(скрипт предупредит о них). Если SSH-алиаса нет:

```powershell
.\publish.ps1 -Server <ssh-user>@<server-ip>
```

Полезные параметры: `-AppDir /srv/xam`, `-Branch main`.

Для первого запуска без локального `.env.production` скрипт не станет его
загружать и потребует, чтобы файл уже был на сервере.

## 6. Ручной запуск на сервере

```bash
cd /opt/xam/xam_call_service/deploy/production
docker compose --env-file .env.production -f docker-compose.prod.yml up -d --build
docker compose --env-file .env.production -f docker-compose.prod.yml ps
docker compose --env-file .env.production -f docker-compose.prod.yml logs -f caddy
```

Остановка:

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml down
```

Данные лежат в named volumes: `xam_postgres_data`, `xam_uploads_data`,
`xam_calls_data`, `xam_caddy_data`, `xam_caddy_config`. Команда `down` их не
удаляет; `down -v` удаляет.

## 7. Проверка после деплоя

```bash
curl -fsS https://example.com/healthz
curl -fsS https://backend.example.com/health
curl -fsS https://callservice.example.com/health
```

Ожидаемо: `ok`, `{"status":"ok"}` и `{"status":"ok","service":"xam_call_service"}`.

Дополнительно:

- `https://callservice.example.com/client/` — встроенный WebRTC-клиент;
- `https://backend.example.com/docs` — Swagger backend;
- `https://callservice.example.com/docs` — Swagger сервиса звонков.

Для звонков нужен HTTPS (secure context) и рабочий TURN. Проверьте, что
браузер получает ICE-серверы с адресом реального IP, а в диагностике есть
`relay`-кандидаты.

## 8. Обновление

```powershell
.\publish.ps1
```

Скрипт делает `git reset --hard origin/main` в каждом репозитории. Не храните
на сервере незакоммиченные изменения в этих трёх каталогах. Секреты и данные
лежат вне git (`.env.production` и Docker volumes).

Пересборка перезапускает контейнеры. Активные звонки и конференции при этом
завершаются: их состояние хранится в памяти процесса `xam_call_service`.

## 9. Частые проблемы

| Симптом | Причина / решение |
| --- | --- |
| Caddy не выпускает сертификат | DNS ещё не указывает на сервер, закрыт `80/tcp`, или домен не совпадает с `Caddyfile` |
| `port is already allocated` | На сервере уже запущены старые контейнеры на `80/443/3478`; остановите их |
| `permission denied` при git | Нет deploy key/токена для приватного репозитория |
| Нет звука/видео через интернет | `SERVER_IP`/`TURN_EXTERNAL_IP` — заглушка, либо закрыты `3478` и `49160-49300/udp` |
| `401` на всех запросах | `SECRET_KEY` в call service не совпадает с backend |
| `database does not exist` | Проверьте `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` и логи `xam_db` |
| Браузер не даёт камеру | Открывайте сайт по HTTPS, не по HTTP/IP |
