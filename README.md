# ferz-bot

Telegram-бот приёма заявок для сайта Эдуарда Торгашова (Ferz). Python 3.12–3.14, aiogram 3, long polling.
Подробный README — на этапе 2; решения и история — в `STATUS.md`.

## Запуск

```bash
# 1. Окружение (зависимости зафиксированы в uv.lock)
uv sync                       # создаст .venv и поставит всё, включая dev-инструменты
# без uv:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. Настройки
cp .env.example .env          # впишите BOT_TOKEN и ADMIN_ID

# 3. Старт (Ctrl+C — остановка)
.venv/bin/python -m bot
```

Перед первым запуском откройте своего бота в Telegram и нажмите /start — иначе бот не сможет прислать вам заявку.

## Проверки

```bash
.venv/bin/python -m pytest    # тесты без сети и без настоящего токена
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy                # строгая проверка типов пакета bot
```

После изменения зависимостей: `uv lock && uv export --format requirements-txt --no-dev --no-hashes -o requirements.txt`.

## Структура

```
bot/
  __main__.py       точка входа: проверка токена, polling, остановка
  app.py            сборка Bot и Dispatcher, зависимости (workflow_data)
  config.py         настройки из .env (pydantic-settings, SecretStr)
  texts.py          все тексты бота
  models.py         заявка, черновик, типы проекта
  states.py         шаги диалога (FSM)
  callbacks.py      callback_data кнопок
  keyboards.py      inline-клавиатуры
  drafts.py         черновик заявки в хранилище FSM
  middlewares.py    только личные чаты
  logging_setup.py  логи без токена и без текстов заявок
  handlers/         start, common (/help, /cancel, fallback), application (диалог), admin, errors
  services/         validation, formatting (HTML-экранирование), notifier, rate_limit, storage
tests/              pytest: сценарии через dp.feed_update с подменённой сессией
```
