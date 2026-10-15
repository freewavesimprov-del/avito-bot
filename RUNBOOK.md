# RUNBOOK — поднять бота в проде «один пуш и взлетел»

Документ написан с расчётом на одного человека с ssh-доступом к VPS, у которого
к утру есть домен и желание не сломать боевой Битрикс. Все опасные шаги делаются
в порядке, при котором бот **физически не сможет ответить клиенту**, пока ты сам
не активируешь его на последнем шаге.

---

## 0. Что нужно подготовить заранее (10–15 минут)

| Что | Где | Зачем |
|---|---|---|
| Поддомен → A-запись на IP VPS | DNS-провайдер | Чтобы certbot выдал HTTPS |
| ssh-доступ root на VPS (Debian/Ubuntu 22+) | Сам VPS | Установка |
| Доступ админа в B24 | Битрикс | Создать локальное приложение |
| YC_API_KEY и YC_FOLDER_ID | Уже в .env локально | Перенесём на сервер |

**Поддомен** — придумай и пропиши A-запись, например:
```
avito-bot   A   123.45.67.89   TTL 3600
```
Подожди 5–15 минут, проверь `dig avito-bot.example.ru` — резолвится в IP сервера.

---

## 1. Создать локальное приложение в Bitrix24 (5 минут)

Делаем **до** деплоя, но **установка** приложения произойдёт **после** деплоя —
сервер должен отвечать на момент нажатия «Установить», иначе токены потеряются.

1. В B24: левое меню → **Разработчикам** → **Другое** → **Локальное приложение**
   (если раздела «Разработчикам» нет — включи в «Ещё → Все приложения → Разработчикам»).
2. Заполни:
   - **Тип:** Серверное
   - **Путь вашего обработчика:** `https://avito-bot.example.ru/b24/webhook` ← свой поддомен
   - **Путь для установки:** тот же URL
   - **Использует только API:** ✅
   - **Настройка прав (scope):** **`imbot`**, **`im`**, **`imopenlines`**, **`crm`**
3. **Сохранить.** Из открывшейся карточки приложения скопировать в `.env`:
   - **Код приложения (`client_id`)** → `B24_CLIENT_ID`
   - **Ключ приложения (`client_secret`)** → `B24_CLIENT_SECRET`

   `access_token` и `application_token` **в карточке не показаны** — они
   придут в payload `ONAPPINSTALL` и автоматически сохранятся нашим сервером
   в `data/b24_install.json` после нажатия «Установить» (шаг 7 ниже).

4. **Не нажимай «Установить» сейчас** — сервер ещё не задеплоен, и B24
   не сможет до него достучаться. Сначала шаги 2–6, потом возвращаемся
   и жмём «Установить».

---

## 2. Установить бота на VPS (одна команда + пара правок)

С локальной машины:

```bash
ssh root@123.45.67.89

# Если репо ещё не залит на git — сделай это (можно приватный) и подставь URL ниже.
# В крайнем случае — rsync исходников вручную в /opt/avito-bot.

REPO_URL=https://github.com/ВАША_ОРГА/avito-bot.git \
bash <(curl -fsSL https://raw.githubusercontent.com/ВАША_ОРГА/avito-bot/main/deploy/install.sh)
```

Скрипт сделает:
- системного пользователя `avitobot`
- клон репо в `/opt/avito-bot`
- venv + установку зависимостей
- systemd unit и nginx-конфиг (но **не запустит** бота — это шаг ниже)

> Если предпочитаешь руками без `install.sh` — открой `deploy/install.sh`,
> там 7 пронумерованных шагов, копипастятся.

---

## 3. Положить `.env` на сервер

```bash
# На VPS, от root:
cp /opt/avito-bot/.env.example /opt/avito-bot/.env
nano /opt/avito-bot/.env
```

Заполни **обязательные** переменные:

```bash
APP_ENV=prod
MOCK_LLM=false

# Bitrix24
B24_DOMAIN=ваш-портал.bitrix24.ru
B24_CLIENT_ID=local.xxxxxxxxxxxx.xxxxxx       # из карточки приложения
B24_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxx   # из карточки приложения
# B24_ACCESS_TOKEN и B24_APPLICATION_TOKEN оставляем ПУСТЫМИ —
# они автоматически сохранятся в data/b24_install.json после переустановки приложения.
B24_ACCESS_TOKEN=
B24_APPLICATION_TOKEN=
# B24_BOT_ID пока не заполняем — получим в шаге 7

# Yandex Cloud
YC_API_KEY=<твой_yc_key>
YC_FOLDER_ID=<твой_folder_id>
YC_MODEL=yandexgpt-lite
```

Поправь права:

```bash
chown avitobot:avitobot /opt/avito-bot/.env
chmod 0600 /opt/avito-bot/.env
```

---

## 4. Сертификат HTTPS

```bash
# На VPS:
nano /etc/nginx/sites-available/avito-bot.conf
# заменить avito-bot.example.ru на свой поддомен (3 раза в файле)

nginx -t && systemctl reload nginx

certbot --nginx -d avito-bot.example.ru
# certbot сам пропишет ssl_certificate / ssl_certificate_key и редирект
```

После certbot — `nginx -t && systemctl reload nginx` ещё раз для перестраховки.

---

## 5. Запустить бота

```bash
systemctl enable --now avito-bot
systemctl status avito-bot       # должно быть Active: active (running)
journalctl -u avito-bot -f       # стримить логи

# Снаружи проверить:
curl -s https://avito-bot.example.ru/health
# {"status":"ok","version":"0.1.0","env":"prod"}
```

Если `/health` отвечает — бэкенд жив. Но ботом он ещё **не является** — он не
зарегистрирован в B24, и B24 о нём ничего не знает.

---

## 6. Установить приложение в B24 (теперь сервер отвечает)

Возвращаемся в Bitrix24 → Разработчикам → твоё локальное приложение «Авито-бот»:

1. Нажать **«Установить»** (или **«Переустановить»**, если кнопка такая).
2. B24 отправит `POST /b24/webhook` с событием `ONAPPINSTALL`,
   в payload — свежие `access_token` / `refresh_token` / `application_token`.
3. Наш сервер автоматически сохранит их в `/opt/avito-bot/data/b24_install.json`.
4. Проверить:
   ```bash
   ls -l /opt/avito-bot/data/b24_install.json
   sudo -u avitobot cat /opt/avito-bot/data/b24_install.json | head
   ```
   Должны быть валидные значения для `domain`, `access_token`, `application_token`.

5. Sanity-check связи:
   ```bash
   sudo -u avitobot /opt/avito-bot/.venv/bin/python -m scripts.b24_check
   ```
   Должно вывести:
   ```
   [ok] install найден: domain=..., app_token=set, access_expired=False
   [ok] profile: ID=1, NAME=Иванов, ADMIN=True
   ```

Если `b24_check` ругается на отсутствие install-файла — значит ONAPPINSTALL
не дошёл (firewall? domain без HTTPS?). Смотри `journalctl -u avito-bot -f` —
там должна быть строка `webhook: ONAPPINSTALL captured`.

---

## 7. Регистрация бота в B24

```bash
sudo -u avitobot /opt/avito-bot/.venv/bin/python -m scripts.register_bot register \
    --webhook-url https://avito-bot.example.ru/b24/webhook \
    --name "Авито-ассистент"
# [ok] BOT_ID = 42
```

Записать `B24_BOT_ID` в `.env`:

```bash
echo "B24_BOT_ID=42" >> /opt/avito-bot/.env
systemctl restart avito-bot
journalctl -u avito-bot -f       # проверить, что не упал
```

**На этом этапе бот зарегистрирован, но в Открытых линиях ещё не назначен** —
он ничего не отвечает клиентам. Безопасный «холостой» режим.

---

## 8. Тестовое сообщение в личный чат

```bash
# Из B24 открой бота «Авито-ассистент» и напиши «привет».
# DIALOG_ID можно увидеть в URL чата (chat<число> или imol|...).
# Альтернатива: отправить тест с сервера руками
sudo -u avitobot /opt/avito-bot/.venv/bin/python -m scripts.b24_check --send chat42 --text "Тест"
```

Если в личке от бота пришло сообщение — связь B24 ↔ сервер ↔ YandexGPT работает.
Логи бота: `journalctl -u avito-bot -f`. Должна быть строка
`handler: {'status':'processed','action':'sent', ...}`.

---

## 9. (Когда РОП скажет «включаем») активировать в Открытой линии

1. В B24: **CRM → Контакт-центр → Открытые линии** → выбрать линию, привязанную
   к Авито-коннектору (ту, в которую завтра должны посыпаться сообщения).
2. **Очередь сотрудников** → поставить **«Авито-ассистент»** **первым** в очереди.
3. **Время реакции на сообщение** → выбрать время, через которое (если бот не
   ответил) запрос пойдёт следующему сотруднику. Рекомендуется **30–60 секунд**.
4. Сохранить.
5. Включить коннектор Avito↔B24 (если не был включён раньше).

С этого момента **первое** сообщение от клиента попадает к боту, бот пытается
ответить через YandexGPT с нашим system prompt + post-filter; на эскалации/
жалобах — выходит, и клиент уезжает к живому менеджеру.

---

## 10. Что мониторить первые сутки

```bash
# Логи в реальном времени:
journalctl -u avito-bot -f

# Сколько диалогов обработано:
sqlite3 /opt/avito-bot/data/bot.db \
    "SELECT direction, COUNT(*) FROM events GROUP BY direction;"

# Что заблокировал post-filter (если что-то — это сигнал что-то докрутить в промпте):
sqlite3 /opt/avito-bot/data/bot.db \
    "SELECT ts, message, json_extract(meta, '$.block_reason')
     FROM events WHERE direction='blocked' ORDER BY ts DESC LIMIT 20;"

# Что эскалировано:
sqlite3 /opt/avito-bot/data/bot.db \
    "SELECT ts, json_extract(meta, '$.reason'), message
     FROM events WHERE direction='escalate' ORDER BY ts DESC LIMIT 20;"
```

---

## 11. Аварийный стоп (если что-то идёт не так)

**Самый быстрый — выключить бота из Открытой линии:**
B24 → Открытые линии → убрать «Авито-ассистент» из очереди. Клиенты сразу
пойдут напрямую к живому менеджеру, ничего ломать не нужно.

**Полностью разрегистрировать:**
```bash
sudo -u avitobot /opt/avito-bot/.venv/bin/python -m scripts.register_bot unregister 42
```

**Остановить сервис:**
```bash
systemctl stop avito-bot
```

---

## Чек-лист «всё ли я сделал»

- [ ] DNS: `dig +short avito-bot.example.ru` показывает IP сервера
- [ ] B24-приложение создано, scope включает `imbot, imopenlines, im, crm`
- [ ] `.env` на сервере, `B24_CLIENT_ID` / `B24_CLIENT_SECRET` заполнены, `chmod 0600`, `chown avitobot:avitobot`
- [ ] `systemctl status avito-bot` → active (running)
- [ ] `curl https://avito-bot.example.ru/health` → 200 OK
- [ ] В B24 нажата «Установить» / «Переустановить»
- [ ] `data/b24_install.json` создан (содержит непустые `access_token`, `application_token`, `domain`)
- [ ] `python -m scripts.b24_check` → profile прошёл
- [ ] `python -m scripts.register_bot register --webhook-url ...` → выдал BOT_ID
- [ ] `B24_BOT_ID=<id>` дописано в `.env`, сервис рестартанут
- [ ] В личном чате бот отвечает на «привет» осмысленным текстом
- [ ] В Открытых линиях бот **пока не первый в очереди** (включаем после согласования с РОП)

---

## Структура `.env` (полный пример для прода)

```bash
APP_ENV=prod
MOCK_LLM=false

B24_DOMAIN=company.bitrix24.ru
B24_ACCESS_TOKEN=xxxxxxxxxxxxxxxx
B24_APPLICATION_TOKEN=yyyyyyyyyyyy
B24_BOT_ID=42

YC_API_KEY=AQVN...
YC_FOLDER_ID=b1g1q31hq82du4le62p6
YC_MODEL=yandexgpt-lite
YC_TIMEOUT_SEC=8

APP_HOST=127.0.0.1
APP_PORT=8000
WEBHOOK_PATH=/b24/webhook
DB_PATH=/opt/avito-bot/data/bot.db
LOG_LEVEL=INFO
DEBOUNCE_SEC=10
```
