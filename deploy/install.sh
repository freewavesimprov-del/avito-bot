#!/usr/bin/env bash
# Bootstrap-скрипт для установки бота на чистый Debian/Ubuntu VPS.
# Запускать от root: bash install.sh
#
# Делает:
#   - системного пользователя avitobot
#   - клонит репо в /opt/avito-bot (если нужно — поменяй REPO_URL ниже)
#   - ставит python3.11, venv, deps
#   - кладёт systemd unit + nginx site
#   - НЕ запускает сервис и НЕ выпускает сертификат — это финальный шаг по RUNBOOK
#
# Идемпотентно (можно запускать повторно).

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/YOUR/avito-bot.git}"
APP_DIR="/opt/avito-bot"
USER="avitobot"

log() { echo "[install] $*"; }

# 1. Пакеты
log "пакеты"
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3.11 python3.11-venv python3-pip \
    nginx certbot python3-certbot-nginx \
    git curl ca-certificates sqlite3

# 2. Пользователь
if ! id -u "$USER" >/dev/null 2>&1; then
    log "создаю пользователя $USER"
    useradd --system --create-home --shell /usr/sbin/nologin "$USER"
fi

# 3. Клон/обновление репо
if [ ! -d "$APP_DIR/.git" ]; then
    log "клонирую репо в $APP_DIR"
    git clone "$REPO_URL" "$APP_DIR"
else
    log "обновляю репо"
    git -C "$APP_DIR" pull --ff-only
fi

# 4. venv + deps
log "venv + зависимости"
sudo -u "$USER" python3.11 -m venv "$APP_DIR/.venv"
sudo -u "$USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip -q
sudo -u "$USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q

# 5. data dir
install -d -o "$USER" -g "$USER" -m 0750 "$APP_DIR/data"

# 6. systemd
log "systemd unit"
install -m 0644 "$APP_DIR/deploy/avito-bot.service" /etc/systemd/system/avito-bot.service
systemctl daemon-reload

# 7. nginx
if [ ! -f /etc/nginx/sites-available/avito-bot.conf ]; then
    log "nginx site (шаблон, поправь server_name перед certbot)"
    install -m 0644 "$APP_DIR/deploy/nginx.conf.example" /etc/nginx/sites-available/avito-bot.conf
    ln -sf /etc/nginx/sites-available/avito-bot.conf /etc/nginx/sites-enabled/avito-bot.conf
    install -d -o www-data -g www-data /var/www/letsencrypt
    nginx -t && systemctl reload nginx
fi

cat <<EOF

[install] готово.

Дальше по RUNBOOK.md:
  1. Заполнить $APP_DIR/.env (B24_DOMAIN, B24_ACCESS_TOKEN, B24_APPLICATION_TOKEN, YC_API_KEY, YC_FOLDER_ID).
     Пример:  cp $APP_DIR/.env.example $APP_DIR/.env && nano $APP_DIR/.env
     Права:   chown $USER:$USER $APP_DIR/.env && chmod 0600 $APP_DIR/.env
  2. Поправить server_name в /etc/nginx/sites-available/avito-bot.conf на свой поддомен.
  3. Выпустить сертификат:  certbot --nginx -d avito-bot.example.ru
  4. systemctl enable --now avito-bot
  5. curl https://avito-bot.example.ru/health   → должен вернуть {"status":"ok",...}
  6. Только после этого — запустить scripts/register_bot.py register --webhook-url https://avito-bot.example.ru/b24/webhook
EOF
