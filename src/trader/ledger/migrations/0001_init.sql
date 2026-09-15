-- Initial schema (spec "データモデル"). Amounts/quantities are TEXT canonical
-- Decimal strings (N-1); timestamps are ISO 8601 UTC TEXT.

CREATE TABLE sources (
    source_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    record_count INTEGER NOT NULL
);

CREATE TABLE mentions (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources (source_id),
    external_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    text_excerpt TEXT NOT NULL,
    url TEXT,
    raw_sha256 TEXT NOT NULL,
    UNIQUE (source_id, external_id, ticker)
);

CREATE TABLE rule_sets (
    sha256 TEXT PRIMARY KEY,
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    capital_usd TEXT NOT NULL,
    content_yaml TEXT NOT NULL
);

CREATE TABLE cycles (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    mode TEXT NOT NULL,
    outcome TEXT,
    error_summary TEXT
);

CREATE TABLE decisions (
    id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL REFERENCES cycles (id),
    decided_at TEXT NOT NULL,
    mode TEXT NOT NULL,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL,
    origin TEXT NOT NULL,
    confidence TEXT,
    rationale TEXT NOT NULL,
    evidence_mention_ids TEXT NOT NULL,
    llm_model TEXT,
    prompt_sha256 TEXT,
    response_sha256 TEXT,
    rule_set_sha256 TEXT NOT NULL REFERENCES rule_sets (sha256),
    rule_check TEXT NOT NULL,
    rule_check_reason TEXT NOT NULL,
    proposed_notional TEXT,
    reference_price TEXT
);

CREATE TABLE approvals (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES decisions (id),
    approver TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE orders (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES decisions (id),
    approval_id TEXT NOT NULL REFERENCES approvals (id),
    client_order_id TEXT NOT NULL UNIQUE,
    broker_order_id TEXT,
    mode TEXT NOT NULL,
    side TEXT NOT NULL,
    qty TEXT NOT NULL,
    order_type TEXT NOT NULL,
    status TEXT NOT NULL,
    submitted_at TEXT,
    last_error TEXT
);

CREATE TABLE fills (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders (id),
    filled_at TEXT NOT NULL,
    qty TEXT NOT NULL,
    price TEXT NOT NULL,
    fee TEXT NOT NULL
);

CREATE TABLE positions (
    ticker TEXT PRIMARY KEY,
    qty TEXT NOT NULL,
    avg_cost TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    high_watermark TEXT NOT NULL,
    partial_tp_done INTEGER NOT NULL
);

CREATE TABLE trades (
    id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT NOT NULL,
    entry_decision_id TEXT NOT NULL REFERENCES decisions (id),
    exit_decision_ids TEXT NOT NULL,
    exit_reason TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    fees TEXT NOT NULL,
    holding_days INTEGER NOT NULL
);

CREATE TABLE equity_snapshots (
    snapshot_date TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    cash TEXT NOT NULL,
    positions_value TEXT NOT NULL,
    equity TEXT NOT NULL,
    peak_equity TEXT NOT NULL,
    drawdown_pct TEXT NOT NULL,
    taken_at TEXT NOT NULL
);

CREATE TABLE engine_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
