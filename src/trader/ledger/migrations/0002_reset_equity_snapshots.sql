-- Equity is now derived from `capital_usd` plus the ledger's own fills
-- (see `portfolio_repository.ledger_cash`), never from the broker's account
-- balance. Rows written before this change carry the broker balance (Alpaca
-- paper: $100,000) as `cash`, so their `peak_equity` would make every
-- capital-based snapshot look like a ~97% drawdown and trip the kill switch.
-- They cannot be recomputed (the broker's starting balance is unknown here),
-- so they are discarded; the next cycle restarts the series at `capital_usd`.
DELETE FROM equity_snapshots;
