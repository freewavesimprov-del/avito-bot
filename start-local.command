#!/bin/bash
# macOS/Linux: bash start-local.command
set -euo pipefail
cd -- "$(dirname -- "$0")"

runtime=""
for candidate in python3.12 python3.11 python3.13 python3.14 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
    runtime="$candidate"
    break
  fi
done
if [ -z "$runtime" ]; then
  echo 'Нужен Python 3.11 или новее. Системный Python 3.9 из Xcode не подходит.'
  echo 'Если установлен Homebrew: brew install python@3.12'
  echo 'Или установите Python 3.12 с https://www.python.org/downloads/macos/'
  echo 'Затем снова выполните: bash start-local.command'
  exit 1
fi

# Keep the user's old .venv intact, including an incompatible Python 3.9 venv.
if [ ! -x .venv-local/bin/python ]; then
  "$runtime" -m venv .venv-local
fi
.venv-local/bin/python -c 'import sys; assert sys.version_info >= (3,11), "Папка .venv-local создана старым Python; используйте новое окружение Python 3.11+"'
.venv-local/bin/python -m pip install -r requirements.txt

# Do not overwrite any existing environment file or expose existing credentials.
if [ ! -f .env.local ]; then
  .venv-local/bin/python - <<'PY'
import os
import secrets
content = '''APP_ENV=prod
MOCK_LLM=false
ADMIN_USER=admin
ADMIN_PASSWORD={password}
CATALOG_PATH=./data/pilot_skus.json
DB_PATH=./data/local.db
OL_QUEUE_ENABLE=false
LEAD_ROUTER_ENABLE=false
OL_WATCHDOG_ENABLE=false
AVITO_WATCHER_ENABLE=false
TG_NOTIFY_ENABLE=false
'''.format(password=secrets.token_urlsafe(18))
with os.fdopen(os.open('.env.local', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
    stream.write(content)
print('Создан .env.local. Логин: admin. Пароль указан в ADMIN_PASSWORD внутри этого файла.')
PY
fi
echo 'Откройте http://127.0.0.1:8000/admin/catalog'
echo 'Логин и пароль находятся в .env.local (на macOS: open -e .env.local).'
exec .venv-local/bin/python -m uvicorn app.main:app --env-file .env.local --host 127.0.0.1 --port 8000
