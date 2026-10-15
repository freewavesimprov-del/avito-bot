#!/usr/bin/env bash
# Мониторинг avito-bot: проверяет то, что реально ломалось в проде.
# Запускается systemd-таймером раз в 5 минут.
#
# Проверки:
#   1. DNS   — домен резолвится через внешний резолвер в правильный IP
#              (именно это сломалось 20.07: запись пропала, B24 не мог достучаться)
#   2. TLS   — сертификат не истекает в ближайшие 7 дней
#   3. APP   — сервис отвечает на /health
#   4. AVITO — Avito API не отдаёт 402 (подписка «Максимальная» жива)
#   5. B24   — события от Битрикса приходили за последние N часов
#
# Алерты шлются в Telegram ТОЛЬКО при смене состояния (OK→FAIL и FAIL→OK),
# чтобы не спамить одинаковыми сообщениями каждые 5 минут.

set -uo pipefail

# --- Конфиг ---------------------------------------------------------------
DOMAIN="avito-bot.n8nrgimprovise.space"
EXPECTED_IP="81.85.78.109"
LOCAL_HEALTH="http://127.0.0.1:8000/health"
SERVICE="avito-bot"
STATE_FILE="/var/lib/avito-bot-monitor/state"
CERT_MIN_DAYS=7
# Сколько часов тишины от B24 считать подозрительным.
# 48ч — с запасом: при малом потоке клиентов сутки без обращений это норма.
B24_SILENCE_HOURS=48

# Telegram (заполняется при установке)
TG_TOKEN="${TG_TOKEN:-}"
TG_CHAT_ID="${TG_CHAT_ID:-}"

mkdir -p "$(dirname "$STATE_FILE")"

# --- Проверки -------------------------------------------------------------
problems=()

# 1. DNS через внешний резолвер (обходим локальный кэш)
resolved="$(dig +short A "$DOMAIN" @8.8.8.8 2>/dev/null | head -1)"
if [[ -z "$resolved" ]]; then
    problems+=("DNS: запись $DOMAIN не резолвится (NXDOMAIN) — B24 не сможет достучаться до вебхука")
elif [[ "$resolved" != "$EXPECTED_IP" ]]; then
    problems+=("DNS: $DOMAIN указывает на $resolved, ожидался $EXPECTED_IP")
fi

# 2. Срок сертификата. Обращаемся к 127.0.0.1 с нужным SNI — так мы читаем
#    ровно тот сертификат, который Caddy отдаёт наружу, минуя hairpin-NAT.
cert_end="$(echo | openssl s_client -servername "$DOMAIN" -connect 127.0.0.1:443 2>/dev/null \
            | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)"
if [[ -z "$cert_end" ]]; then
    problems+=("TLS: не удалось прочитать сертификат для $DOMAIN")
else
    cert_epoch="$(date -d "$cert_end" +%s 2>/dev/null || echo 0)"
    now_epoch="$(date +%s)"
    days_left=$(( (cert_epoch - now_epoch) / 86400 ))
    if (( cert_epoch == 0 )); then
        problems+=("TLS: не удалось разобрать дату истечения сертификата")
    elif (( days_left < CERT_MIN_DAYS )); then
        problems+=("TLS: сертификат истекает через $days_left дн. — Caddy не смог продлить")
    fi
fi

# 3. Сервис жив и отвечает
if ! systemctl is-active --quiet "$SERVICE"; then
    problems+=("APP: systemd-юнит $SERVICE не запущен")
else
    health_code="$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$LOCAL_HEALTH" 2>/dev/null)"
    if [[ "$health_code" != "200" ]]; then
        problems+=("APP: /health вернул HTTP $health_code (ожидался 200)")
    fi
fi

# 4. Avito API: 402 = слетела подписка «Максимальная», бот ослеп
if journalctl -u "$SERVICE" --since '15 min ago' --no-pager 2>/dev/null \
   | grep -q '402 Payment Required'; then
    problems+=("AVITO: API отдаёт 402 Payment Required — проверь подписку «Максимальная» в Avito Pro")
fi

# 5. Тишина от B24. Ловит случай «внешне всё зелёное, а события не идут».
last_event="$(journalctl -u "$SERVICE" --since "${B24_SILENCE_HOURS} hours ago" --no-pager 2>/dev/null \
              | grep -c 'POST /b24/webhook')"
if [[ "${last_event:-0}" == "0" ]]; then
    problems+=("B24: ни одного события за ${B24_SILENCE_HOURS}ч — возможно, отвалился коннектор Avito в Контакт-центре")
fi

# --- Сравнение с прошлым состоянием --------------------------------------
prev_state="$(cat "$STATE_FILE" 2>/dev/null || echo 'OK')"

if (( ${#problems[@]} > 0 )); then
    cur_state="FAIL"
    message="🔴 avito-bot: обнаружены проблемы%0A%0A"
    for p in "${problems[@]}"; do
        message+="• ${p}%0A"
    done
    message+="%0Aсервер: ${EXPECTED_IP}"
else
    cur_state="OK"
    message="🟢 avito-bot: всё восстановилось, проверки проходят"
fi

# Шлём только при смене состояния
if [[ "$cur_state" != "$prev_state" ]]; then
    if [[ -n "$TG_TOKEN" && -n "$TG_CHAT_ID" ]]; then
        curl -s -m 15 -o /dev/null \
            --data-urlencode "chat_id=${TG_CHAT_ID}" \
            --data "text=${message}" \
            "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" || true
    fi
    logger -t avito-bot-monitor "state ${prev_state} -> ${cur_state}: ${problems[*]:-all checks passed}"
fi

echo "$cur_state" > "$STATE_FILE"

# Для ручного запуска — печатаем результат
if (( ${#problems[@]} > 0 )); then
    printf 'FAIL\n'
    printf '  - %s\n' "${problems[@]}"
    exit 1
fi
printf 'OK — все проверки пройдены\n'
