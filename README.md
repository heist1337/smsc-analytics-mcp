# SMSC MCP

[![Check](https://github.com/heist1337/smsc-analytics-mcp/actions/workflows/check.yml/badge.svg)](https://github.com/heist1337/smsc-analytics-mcp/actions/workflows/check.yml)
[![FastMCP](https://img.shields.io/badge/FastMCP-4.1.6-blue)](https://gofastmcp.com/)
[![MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.12%2B-blue)

Чтение SMSC и аналитика расходов через **FastMCP**.
Состояние хранится в одном приватном **SQLite** файле.
Отправки SMS, платежей, платных проверок и изменений аккаунта в коде нет.

## Быстрый старт

Установить [uv](https://docs.astral.sh/uv/getting-started/installation/), затем:

```bash
git clone https://github.com/heist1337/smsc-analytics-mcp.git
cd smsc-analytics-mcp
cp .env.example .env
# Записать API-ключ SMSC в приватный .env.
uv run --locked --env-file .env smsc-mcp
```

По умолчанию используется **stdio**: сервер запускается самим MCP-клиентом.
Python и зависимости устанавливает uv.
Готовый шаблон подключения: [mcp.example.json](mcp.example.json).

## Инструменты

| Инструмент | Действие |
| --- | --- |
| `smsc_read` | Каталог разрешённых чтений; живые чтения и обезличенные локальные снимки |
| `smsc_history` | Одна страница исходящих с явным продолжением |
| `smsc_cost_report` | Анализ одной живой страницы; не итог большого периода |
| `smsc_refresh` | Одна страница в локальную историю, checkpoint и защита конкуренции |
| `smsc_analytics` | Аналитика SQLite без API-ключа и запросов к SMSC |

Начать с `smsc_read(operation="catalog")`: ключ для каталога не нужен.
Даты — `DD.MM.YYYY`. Проверять `complete`, `next_cursor`, `coverage` и
`truncated`; пустой или неполный кеш не означает отсутствие расходов.
Стоимость сообщений отличается от всех списаний со счёта. Валюты не смешиваются,
суммы считаются Decimal. Тексты и персональные данные по умолчанию скрыты.

## Настройки

| Переменная | Назначение |
| --- | --- |
| `SMSC_API_TOKEN` | API-ключ SMSC; alias `SMSC_API_KEY` |
| `SMSC_CURRENCY` | Подтверждённая валюта аккаунта; без настройки `unknown` |
| `SMSC_TIMEZONE` | По умолчанию `Europe/Moscow` |
| `SMSC_DATABASE` | По умолчанию `~/.local/share/smsc-mcp/state.sqlite3` |
| `SMSC_HISTORY_CURRENCY_VERIFIED` | По умолчанию false; подтвердить валюту поля cost перед накопленной загрузкой |
| `SMSC_STORE_MESSAGE_TEXT` | По умолчанию false; хранение текстов требует отдельного решения |
| `SMSC_REDACTION_KEY` | Необязательный прежний HMAC-секрет; иначе создаётся и сохраняется в SQLite |
| `SMSC_SERVER` | `primary` или `backup`; произвольный endpoint запрещён |
| `SMSC_CONFIG_FILE` | Необязательный приватный JSON вместо env; поддерживает прежние login/psw |

`refresh` принимает только закрытые дни и проверенную валюту cost.
Нет автоматической загрузки истории и расписаний. Изменение валюты, зоны или
HMAC-ключа после загрузки блокируется до согласования данных.

Все экземпляры одного аккаунта должны использовать **один SQLite файл**:
в нём общий rolling budget, CAS/checkpoints и leases. Лимит истории —
20 запросов/час с интервалом минимум 21 секунда; неудачные запросы не возвращают
резерв. Другие потребители SMSC на других машинах требуют координации отдельно.

## HTTP и Docker

Для HTTP задать `MCP_TOKEN` длиной от 32 символов:

```bash
uv run --locked --env-file .env smsc-mcp --transport http --port 18092
```

URL: `http://127.0.0.1:18092/mcp/`, `Authorization: Bearer <MCP_TOKEN>`.
Сервер слушает loopback; доступ извне требует приватного транспорта/HTTPS.

```bash
docker build -t smsc-mcp .
docker run --rm -i --env-file .env -v smsc-state:/state smsc-mcp
```

Контейнер работает UID 1000. Том `/state` нужно сохранять между запусками:
бюджет, checkpoints и HMAC-ключ не должны пропадать. Docker Desktop не требуется.

## Перенос и разработка

Остановить процессы и перенести checkout, приватный env и SQLite файл.
Для живого backup использовать штатный SQLite backup API. База содержит
финансовые данные и HMAC-секрет: файл 0600, каталог 0700, вне Git.
Нет зависимости от Personal Context, Windmill, PostgreSQL или другого MCP.

```bash
uv sync --locked
uv run ruff check src tests
uv run python -m unittest discover -b -s tests
uv build
```

[Передача коллегам и проверка](SHARING.md) · [MIT](LICENSE)
