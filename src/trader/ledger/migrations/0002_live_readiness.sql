-- live-readiness: benchmark closes and per-day position marks (LR-13, LR-14).
-- New tables only; existing tables are untouched (NFR-9).

CREATE TABLE benchmark_prices (
    ticker TEXT NOT NULL,
    price_date TEXT NOT NULL,
    close TEXT NOT NULL,
    taken_at TEXT NOT NULL,
    PRIMARY KEY (ticker, price_date)
);

CREATE TABLE position_marks (
    mark_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    qty TEXT NOT NULL,
    avg_cost TEXT NOT NULL,
    mark_price TEXT NOT NULL,
    unrealized_pnl TEXT NOT NULL,
    taken_at TEXT NOT NULL,
    PRIMARY KEY (mark_date, ticker)
);
