# Trading Bot (Binance)

Автоматизированный торговый бот для криптовалютной биржи [Binance](https://www.binance.com/).

## Технологии

- **Python** — язык разработки
- **PostgreSQL 16** — база данных для хранения сделок, ордеров, рыночных данных
- **Docker** — контейнеризация PostgreSQL и pgAdmin
- **python-binance** — официальная библиотека Binance API

## Быстрый старт

### 1. Клонирование

```bash
git clone <repo-url>
cd Проект-Бот
```

### 2. Настройка переменных окружения

```bash
cp .env.example .env
# Отредактируй .env — вставь свои API-ключи Binance и пароли БД
```

### 3. Запуск PostgreSQL

```bash
docker compose up -d
```

После запуска:
- **PostgreSQL** доступен на `localhost:5432`
- **pgAdmin** доступен на [http://localhost:5050](http://localhost:5050)

### 4. Установка Python-зависимостей

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
# source .venv/bin/activate  # Linux/Mac
pip install -r requirements.txt
```

## Структура проекта

```
Проект-Бот/
├── docker/
│   └── postgres/
│       └── init.sql          # SQL-схема БД (создаётся при первом запуске)
├── src/                       # Исходный код бота
├── .env                       # Конфигурация (НЕ коммитить!)
├── .env.example               # Пример конфигурации
├── .gitignore
├── docker-compose.yml         # PostgreSQL + pgAdmin
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
