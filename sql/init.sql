-- ============================================================
-- Trading Bot Database Schema
-- PostgreSQL initialization script
-- ============================================================

-- Trading pairs (e.g., BTCUSDT, ETHUSDT)
CREATE TABLE IF NOT EXISTS trading_pairs (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20)     NOT NULL UNIQUE,      -- e.g. 'BTCUSDT'
    base_asset      VARCHAR(10)     NOT NULL,              -- e.g. 'BTC'
    quote_asset     VARCHAR(10)     NOT NULL,              -- e.g. 'USDT'
    min_qty         NUMERIC(18,8)   DEFAULT 0,
    step_size       NUMERIC(18,8)   DEFAULT 0,
    tick_size       NUMERIC(18,8)   DEFAULT 0,
    is_active       BOOLEAN         DEFAULT TRUE,
    created_at      TIMESTAMPTZ     DEFAULT NOW()
);

-- Bot configuration
CREATE TABLE IF NOT EXISTS bot_config (
    id              SERIAL PRIMARY KEY,
    config_key      VARCHAR(100)    NOT NULL UNIQUE,
    config_value    TEXT            NOT NULL,
    description     TEXT,
    updated_at      TIMESTAMPTZ     DEFAULT NOW()
);

-- Orders (limit, market, stop-loss, etc.)
CREATE TABLE IF NOT EXISTS orders (
    id              SERIAL PRIMARY KEY,
    order_id        VARCHAR(64)     UNIQUE,               -- Binance order ID
    client_order_id VARCHAR(64),                          -- Custom client ID
    symbol          VARCHAR(20)     NOT NULL,
    side            VARCHAR(10)     NOT NULL,              -- BUY / SELL
    order_type      VARCHAR(20)     NOT NULL,              -- LIMIT / MARKET / STOP_LOSS / TAKE_PROFIT
    price           NUMERIC(18,8),
    stop_price      NUMERIC(18,8),
    orig_qty        NUMERIC(18,8)   NOT NULL,
    executed_qty    NUMERIC(18,8)   DEFAULT 0,
    cummulative_quote_qty NUMERIC(18,8) DEFAULT 0,
    status          VARCHAR(20)     NOT NULL DEFAULT 'NEW', -- NEW / PARTIALLY_FILLED / FILLED / CANCELED / REJECTED / EXPIRED
    time_in_force   VARCHAR(10)     DEFAULT 'GTC',         -- GTC / IOC / FPO
    strategy        VARCHAR(50),                           -- Strategy label (e.g. 'ma_cross', 'grid')
    created_at      TIMESTAMPTZ     DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     DEFAULT NOW()
);

-- Trades (fills)
CREATE TABLE IF NOT EXISTS trades (
    id              SERIAL PRIMARY KEY,
    trade_id        VARCHAR(64)     UNIQUE,               -- Binance trade ID
    order_id        VARCHAR(64)     NOT NULL,              -- Related order
    symbol          VARCHAR(20)     NOT NULL,
    side            VARCHAR(10)     NOT NULL,
    price           NUMERIC(18,8)   NOT NULL,
    qty             NUMERIC(18,8)   NOT NULL,
    quote_qty       NUMERIC(18,8)   NOT NULL,              -- price * qty
    commission      NUMERIC(18,8)   DEFAULT 0,
    commission_asset VARCHAR(10)    DEFAULT 'USDT',
    realized_pnl    NUMERIC(18,8)   DEFAULT 0,
    is_buyer        BOOLEAN         DEFAULT FALSE,
    is_maker        BOOLEAN         DEFAULT FALSE,
    strategy        VARCHAR(50),
    trade_time      TIMESTAMPTZ     NOT NULL,
    created_at      TIMESTAMPTZ     DEFAULT NOW()
);

-- Positions (open positions tracking)
CREATE TABLE IF NOT EXISTS positions (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20)     NOT NULL,
    side            VARCHAR(10)     NOT NULL,              -- LONG / SHORT
    entry_price     NUMERIC(18,8)   NOT NULL,
    quantity        NUMERIC(18,8)   NOT NULL,
    current_price   NUMERIC(18,8),
    unrealized_pnl  NUMERIC(18,8)   DEFAULT 0,
    realized_pnl    NUMERIC(18,8)   DEFAULT 0,
    stop_loss       NUMERIC(18,8),
    take_profit     NUMERIC(18,8),
    status          VARCHAR(20)     NOT NULL DEFAULT 'OPEN', -- OPEN / CLOSED
    strategy        VARCHAR(50),
    opened_at       TIMESTAMPTZ     DEFAULT NOW(),
    closed_at       TIMESTAMPTZ
);

-- Market data (OHLCV candles)
CREATE TABLE IF NOT EXISTS market_data (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20)     NOT NULL,
    interval        VARCHAR(10)     NOT NULL,              -- 1m, 5m, 15m, 1h, 4h, 1d, etc.
    open_time       TIMESTAMPTZ     NOT NULL,
    close_time      TIMESTAMPTZ     NOT NULL,
    open            NUMERIC(18,8)   NOT NULL,
    high            NUMERIC(18,8)   NOT NULL,
    low             NUMERIC(18,8)   NOT NULL,
    close           NUMERIC(18,8)   NOT NULL,
    volume          NUMERIC(18,8)   NOT NULL,
    quote_volume    NUMERIC(18,8),
    trades_count    INTEGER,
    created_at      TIMESTAMPTZ     DEFAULT NOW(),
    UNIQUE (symbol, interval, open_time)
);

-- Bot activity logs
CREATE TABLE IF NOT EXISTS bot_logs (
    id              SERIAL PRIMARY KEY,
    level           VARCHAR(10)     NOT NULL DEFAULT 'INFO', -- INFO / WARN / ERROR / DEBUG
    category        VARCHAR(50),                            -- e.g. 'trade', 'api', 'strategy', 'system'
    message         TEXT            NOT NULL,
    metadata        JSONB,                                 -- Extra JSON data
    created_at      TIMESTAMPTZ     DEFAULT NOW()
);

-- Account snapshots (periodic balance tracking)
CREATE TABLE IF NOT EXISTS account_snapshots (
    id              SERIAL PRIMARY KEY,
    total_balance   NUMERIC(18,8)   NOT NULL,              -- In USDT equivalent
    available_balance NUMERIC(18,8) NOT NULL,
    locked_balance  NUMERIC(18,8)   DEFAULT 0,
    balances_json   JSONB,                                 -- Full breakdown per asset
    snapshot_time   TIMESTAMPTZ     DEFAULT NOW()
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_orders_symbol        ON orders(symbol);
CREATE INDEX IF NOT EXISTS idx_orders_status        ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created       ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_trades_symbol        ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_time          ON trades(trade_time);
CREATE INDEX IF NOT EXISTS idx_positions_status     ON positions(status);
CREATE INDEX IF NOT EXISTS idx_market_data_lookup   ON market_data(symbol, interval, open_time);
CREATE INDEX IF NOT EXISTS idx_bot_logs_created     ON bot_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_bot_logs_level       ON bot_logs(level);
