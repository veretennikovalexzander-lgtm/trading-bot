# Trading Bot (Binance)

Автоматизированный торговый бот для криптовалютной биржи [Binance](https://www.binance.com/).

## Технологии

- **Python 3.11+** — язык разработки
- **PostgreSQL 16** — база данных
- **DBeaver** — GUI для работы с БД
- **python-binance** — официальная библиотека Binance API

## Быстрый старт

### 1. Клонирование

```bash
git clone git@github.com:veretennikovalexzander-lgtm/trading-bot.git
cd trading-bot
```

### 2. Установка PostgreSQL

Скачай и установи [PostgreSQL 16](https://www.enterprisedb.com/downloads/postgresql-postgresql-downloads).

При установке задай:
- Пароль для `postgres`: `changeme`
- Порт: `5432`

После установки создай базу и пользователя через **DBeaver** или **pgAdmin**:

```sql
CREATE USER bot_user WITH PASSWORD 'changeme';
CREATE DATABASE trading_bot OWNER bot_user;
```

Затем импортируй схему:

```bash
psql -U bot_user -d trading_bot -f sql/init.sql
```
*(или открой `sql/init.sql` в DBeaver и выполни)*

### 3. Настройка переменных окружения

```bash
cp .env.example .env
# Отредактируй .env — вставь свои API-ключи Binance
```

### 4. Установка Python-зависимостей

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
```

## Структура проекта

```
trading-bot/
├── sql/
│   └── init.sql              # SQL-схема БД
├── src/                       # Исходный код бота
├── .env.example               # Пример конфигурации
├── .gitignore
├── requirements.txt           # Python-зависимости
└── README.md
```

## Схема базы данных

| Таблица | Назначение |
|---|---|
| `trading_pairs` | Торговые пары (BTCUSDT, ETHUSDT, ...) |
| `bot_config` | Настройки бота (ключ-значение) |
| `orders` | Все ордера (лимитные, рыночные, стоп-лосс) |
| `trades` | Исполненные сделки (филлы) |
| `positions` | Открытые/закрытые позиции с PnL |
| `market_data` | OHLCV-свечи для анализа |
| `bot_logs` | Логи работы бота |
| `account_snapshots` | Снапшоты баланса аккаунта |

## Лицензия

MIT
