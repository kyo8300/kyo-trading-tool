# Plan: live-readiness

元 spec: `specs/live-readiness/spec.md`（approved_at: 2026-10-06）
元 intent: `specs/live-readiness/intent.md`（approved_at: 2026-10-06）
親 plan: `specs/ai-auto-trader/plan.md`（タスク粒度・書式の先例）
作成日: 2026-10-06
状態: draft

> **kyo へ（これが自律ループ前の最後の介入点）**
> - タスクは 12 個。縦切り（1 タスク = 1 つの動く経路）で、依存の薄いものは並列グループにまとめた。
> - spec の構成図に無い変更が 3 つある。いずれも既存パターンに合わせるための最小追加で、機能追加ではない:
>   1. `domain/models.py` に `BenchmarkPrice` / `PositionMark` の frozen dataclass を足す（ledger の repository は domain オブジェクトを返す既存規約に合わせる。`Position` は触らない = NFR-5）
>   2. `ledger/portfolio_repository.py` に `list_halted_cycles(conn)` を足す（LR-7 のキルスイッチ回数。`report/` から生 SQL を書かず ledger に置く）
>   3. `engine/cycle.py` の `_acquire_lock` / `_release_lock` を公開名にして `manual_close.py` から再利用する（LR-19「同じロックファイル」。新ファイルは増やさない）
> - 親 spec `specs/ai-auto-trader/spec.md` の Q5 行は builder が T-11 で書き換える（LR-23 / AC-24。planner は触らない）。
> - 既存コードで確認できなかった前提は「リスク」に書いた。特に **リスク 1（`cli.py` の行数）** と **リスク 2（AC-11〜13 の `-k` 部分一致）** は builder / tester の作業に直接効く。

## 変更ファイル

### `src/trader/`

| ファイル | 種別 | 目的 |
|---|---|---|
| `src/trader/domain/models.py` | 変更 | `Origin.manual`、`ExitReason.manual` を追加（LR-16, D-5）。`BenchmarkPrice(ticker, price_date, close, taken_at)` / `PositionMark(mark_date, ticker, qty, avg_cost, mark_price, unrealized_pnl, taken_at)` の frozen dataclass を追加 |
| `src/trader/ledger/migrations/0002_live_readiness.sql` | 新規 | `benchmark_prices` / `position_marks` の `CREATE TABLE`（既存テーブル変更なし、NFR-9） |
| `src/trader/ledger/benchmark_repository.py` | 新規 | `upsert_benchmark_price` / `list_benchmark_prices(ticker)` / `upsert_position_mark` / `list_position_marks(mark_date)` / `delete_position_mark(mark_date, ticker)`。`?` プレースホルダのみ、冪等 upsert（NFR-4） |
| `src/trader/ledger/portfolio_repository.py` | 変更 | `list_halted_cycles(conn) -> tuple[HaltedCycle, ...]`（`outcome='halted'` かつ `error_summary <> 'engine is halted'`。LR-7） |
| `src/trader/market/data_provider.py` | 変更 | Protocol に `daily_bars_since(ticker, start: date) -> Result[tuple[Bar, ...], MarketError]` を追加（LR-12） |
| `src/trader/market/alpaca_data.py` | 変更 | `daily_bars_since` 実装。`StockBarsRequest(start=datetime.combine(start, time.min, tzinfo=UTC))`、既存 `_to_bar` 検証、既存 `_call_with_retry`。`daily_bars` と共通の `_fetch_bars` に寄せて重複を避ける |
| `src/trader/market/fake_data.py` | 変更 | `daily_bars_since` 実装（`bars[ticker]` を `date >= start` で絞る。`failing(["daily_bars_since"])` に対応） |
| `src/trader/engine/execution.py` | 新規 | `ExecutionDeps`（mode / broker / clock / sleep / poll_timeout_s / poll_interval_s）と `execute_decisions(deps, conn, decisions, exit_reasons, market_clock) -> ExecutionSummary`。`cycle._execute_passed_decisions` の移設。`resolve_approval` / `order_executor.execute` / `poll_and_settle` を呼ぶ唯一の場所（LR-17） |
| `src/trader/engine/benchmark.py` | 新規 | `BENCHMARK_TICKER = "SPY"`、`benchmark_start(conn, now) -> date`（最古 snapshot 日 or 当日 ET）、`record_benchmark(conn, market, start, now) -> Result[BenchmarkOutcome, BenchmarkError]`（終値 ≤ 0 は記録せず件数を返す。LR-13） |
| `src/trader/engine/manual_close.py` | 新規 | `CloseDeps`、`CloseOutcome`、`CloseError`、`close_position(deps, ticker, reason) -> Result[CloseOutcome, CloseError]`（LR-15〜LR-22） |
| `src/trader/engine/cycle.py` | 変更 | `_execute_passed_decisions` を削除し `execution.execute_decisions` を呼ぶ。`market_clock` 成功直後に `record_benchmark` を呼び、失敗を `CycleOutcome.benchmark_error`（既定 `None`）に入れる。`_acquire_lock` / `_release_lock` を `acquire_lock` / `release_lock` に改名して公開 |
| `src/trader/engine/holdings.py` | 変更 | 価格を取れた保有ごとに `position_marks` を upsert（`mark_date` = `loss_limits.trading_day(now)`）。upsert 失敗は `HoldingsError`（LR-14） |
| `src/trader/engine/fills.py` | 変更 | `_settle_fill` の `delete_position` 直後に `delete_position_mark(conn, trading_day(fill.filled_at), ticker)`（LR-14） |
| `src/trader/report/readiness.py` | 新規 | 純粋関数 `evaluate_readiness(inputs: ReadinessInputs) -> Readiness`。`Verdict`（met / unmet / undetermined / needs_review）、条件 1〜5 の `ConditionResult`、`P` / `P_ex` / `P_net` / `B` / `DD_cap`。`float` という語を一切書かない（AC-4） |
| `src/trader/report/readiness_inputs.py` | 新規 | `load_readiness_inputs(conn, rule_set, since) -> Result[ReadinessInputs, ReadinessInputsError]`。DB → `ReadinessInputs`（S / E / 銘柄別実現損益 / mark / SPY 終値と代用 / 期間内 fills・注文数 / snapshot 系列 / キルスイッチ / manual 件数）。ネットワークに出ない（NFR-6） |
| `src/trader/report/render.py` | 変更 | `render_readiness(readiness) -> list[str]` を追加し、`render_report(..., readiness: Readiness \| None = None)` で末尾に「## 移行条件（paper → 少額実弾）」を描画（既存引数・既存セクションは不変。AC-19） |
| `src/trader/report/__init__.py` | 変更 | `Readiness` / `evaluate_readiness` / `load_readiness_inputs` / `render_readiness` を export |
| `src/trader/cli.py` | 変更 | (1) `_build_trading_context(command)`: settings → lock 検証 → rules → DB → broker / market を 1 箇所に（run-cycle と close で共用。NFR-8）。(2) `close TICKER [--reason]` コマンド（ルーティングと出力のみ。`TRADER_ENV=test` 拒否）。(3) `report --since YYYY-MM-DD` と移行条件セクションの呼び出し。(4) run-cycle の出力に `benchmark_error` 1 行。ファイル内に `resolve_approval` / `execute_decisions` / `poll_and_settle` の文字列を書かない（AC-23 grep はコメントにも効く） |

### `specs/`

| ファイル | 種別 | 目的 |
|---|---|---|
| `specs/ai-auto-trader/spec.md` | 変更 | Q5 の行（現在 477 行目 `- Q5: ...`）を 1 行のまま「→ **2026-10-06 改訂**: 移行条件は `specs/live-readiness/spec.md` を正とする（『閉じた trade 10 件以上』は廃止）」に更新。旧文は `~~...~~` で同じ行に残す（LR-23, AC-24） |

### `tests/`

| ファイル | 種別 | 目的 |
|---|---|---|
| `tests/unit/domain/test_manual_origin.py` | 新規 | AC-5 |
| `tests/unit/ledger/test_migration_0002.py` | 新規 | AC-6 |
| `tests/unit/ledger/test_benchmark_repository.py` | 新規 | AC-7（`test_sql_safety.py` は既存のまま） |
| `tests/unit/market/test_daily_bars_since.py` | 新規 | AC-8（`test_alpaca_data.py` は既存のまま） |
| `tests/unit/engine/test_shared_execution_path.py` | 新規 | AC-14（`test_no_bypass.py` は既存のまま） |
| `tests/unit/engine/test_benchmark.py` | 新規 | AC-9 |
| `tests/unit/engine/test_marks.py` | 新規 | AC-10 |
| `tests/unit/engine/test_manual_close.py` | 新規 | AC-11, AC-12, AC-13 |
| `tests/unit/report/test_readiness.py` | 新規 | AC-16 |
| `tests/unit/report/test_readiness_inputs.py` | 新規 | AC-17 |
| `tests/unit/report/test_render_readiness.py` | 新規 | AC-18 |
| `tests/unit/test_cli.py` | 変更 | `COMMANDS` に `["close", "--help"]`、`close` の test 環境拒否、`report --since` 不正日付 exit 1、データ無し DB で `report` が exit 0 かつ「移行条件」を含む（AC-20） |
| `tests/integration/test_manual_close_cycle.py` | 新規 | AC-21 |

既存テスト `tests/unit/engine/test_cycle.py` / `test_kill_switch.py` / `test_candidates.py`、`tests/unit/report/test_metrics.py` / `test_live_estimate.py` / `test_render.py`、`tests/integration/test_paper_cycle.py` は**変更しない**（AC-15 / AC-19 / AC-22 のリグレッション基準）。

## 作業順序

1 タスク = 1 commit（tester の RED commit を含めると 2）。各タスク完了時点で `ruff` / `mypy src` / `pytest tests/unit` が通る状態を保つ。

### 概要表

| ID | 内容 | 依存 | 並列可 |
|---|---|---|---|
| T-1 | domain enum + migration 0002 + benchmark_repository | – | T-2, T-3, T-11 と並列可 |
| T-2 | market `daily_bars_since`（Protocol / Alpaca / Fake） | – | T-1, T-3, T-11 と並列可 |
| T-3 | `engine/execution.py` 切り出し + 静的検査 | – | T-1, T-2, T-11 と並列可 |
| T-4 | SPY 記録（`engine/benchmark.py` + cycle 組み込み） | T-1, T-2 | T-5, T-7 と並列可 |
| T-5 | position_marks（holdings upsert / fills delete） | T-1 | T-4, T-7 と並列可 |
| T-6 | `engine/manual_close.py`（close_position） | T-1, T-3, T-5 | T-8 と並列可 |
| T-7 | `report/readiness.py`（純粋判定） | T-1 | T-4, T-5 と並列可 |
| T-8 | `report/readiness_inputs.py`（DB 読み出し） | T-1, T-7 | T-6 と並列可 |
| T-9 | render + `report --since`（CLI） | T-8 | – |
| T-10 | CLI `close` + 依存組み立ての共通化 | T-6, T-9 | – |
| T-11 | 親 spec Q5 の更新 | – | T-1〜T-3 と並列可 |
| T-12 | 統合テスト + カバレッジ + 全 AC 掃討 | T-4, T-10 | – |

### 並列グループ（`build-test` ワークフローに渡す単位）

| グループ | タスク | 前提 |
|---|---|---|
| G1 | T-1, T-2, T-3, T-11 | – |
| G2 | T-4, T-5, T-7 | G1 |
| G3 | T-6, T-8 | G2 |
| G4 | T-9 | G3 |
| G5 | T-10 | G4 |
| G6 | T-12 | G5 |

`cli.py` を触るのは T-9 と T-10 だけで、直列にしてある（衝突回避）。`cycle.py` は T-3 と T-4 が触るが T-3 → T-4 の順（T-4 は T-3 の `execute_decisions` 呼び出しが入った後の `cycle.py` に benchmark 呼び出しを足す）。G2 の 3 タスクは触るファイルが重ならない（T-4: `benchmark.py` / `cycle.py` / `cli.py` は触らない、T-5: `holdings.py` / `fills.py`、T-7: `readiness.py`）。

### T-1: domain enum + migration 0002 + benchmark_repository

- 目的: `manual` の語彙と、SPY 終値 / 銘柄別 mark の置き場を作る（以降の全タスクの土台）
- ファイル: `src/trader/domain/models.py`, `src/trader/ledger/migrations/0002_live_readiness.sql`, `src/trader/ledger/benchmark_repository.py`, `src/trader/ledger/portfolio_repository.py`（`list_halted_cycles` のみ）, `tests/unit/domain/test_manual_origin.py`, `tests/unit/ledger/test_migration_0002.py`, `tests/unit/ledger/test_benchmark_repository.py`
- 内容:
  - `models.py`: `Origin.manual = "manual"`, `ExitReason.manual = "manual"`。`BenchmarkPrice` / `PositionMark`（frozen, slots）。`PositionMark.unrealized_pnl` は `Money`（cent 量子化は `Money.__post_init__` が行う）
  - `0002_live_readiness.sql`: spec「データモデル」どおり。`CREATE TABLE`（`0001` と同じ書き方。`apply_migrations` が `schema_migrations` で冪等化する）
  - `benchmark_repository.py`: `upsert_benchmark_price(conn, ticker, price_date: str, close: Price, taken_at)`、`list_benchmark_prices(conn, ticker) -> tuple[BenchmarkPrice, ...]`（`price_date` 昇順）、`upsert_position_mark(conn, mark: PositionMark)`、`list_position_marks(conn, mark_date: str) -> tuple[PositionMark, ...]`、`delete_position_mark(conn, mark_date: str, ticker: str) -> None`。全て `ON CONFLICT ... DO UPDATE` / `?` プレースホルダ。書き込みは `Result[..., LedgerError]`（既存 repository と同じ流儀、commit しない）
  - `portfolio_repository.py`: `HaltedCycle(id, started_at, error_summary)` と `list_halted_cycles(conn)`（`WHERE outcome = 'halted' AND (error_summary IS NULL OR error_summary <> ?)` に `'engine is halted'` をパラメータで渡す）
- 対応 AC: AC-5, AC-6, AC-7（AC-4 の float 検査は `ledger/` が対象なので本タスクで維持）
- テスト方針（tester）: `test_manual_origin.py`: `origin=manual` の decision と `exit_reason=manual` の trade を insert → `get_decision` / `list_trades` で enum として読み戻せる。`test_migration_0002.py`: (a) 新規 DB に両テーブルがある (b) `0001_init.sql` だけを `executescript` + `schema_migrations` に `0001` を記録した DB に `positions` 1 行と `equity_snapshots` 1 行を入れてから `open_db` → `0002` が適用され、両行がそのまま、`schema_migrations` に `0002` がある (c) 2 回 `open_db` しても Err にならない。`test_benchmark_repository.py`: 同一キー再 upsert で `close` / `mark_price` が上書きされ行数が増えない、`delete_position_mark` で当日行だけ消える（別日は残る）、`list_*` の順序
- 依存: なし
- 並列可: T-2, T-3, T-11

### T-2: market `daily_bars_since`

- 目的: SPY の日足を起点日から取る経路を `MarketDataProvider` の背後に置く
- ファイル: `src/trader/market/data_provider.py`, `src/trader/market/alpaca_data.py`, `src/trader/market/fake_data.py`, `tests/unit/market/test_daily_bars_since.py`
- 内容:
  - Protocol: `daily_bars_since(self, ticker: str, start: date) -> Result[tuple[Bar, ...], MarketError]`（oldest first、`start` を含む）
  - `alpaca_data.py`: `daily_bars` の本体を `_fetch_bars(ticker, start_dt, *, op)` に切り出し、`daily_bars` はそれを `_BARS_LOOKBACK_DAYS` で呼んで末尾 `days` 件、`daily_bars_since` は `datetime.combine(start, time.min, tzinfo=UTC)` で呼んで全件返す。`_to_bar`（pydantic `_RawBar`）と `_call_with_retry`（`RetryPolicy(3, 0.5, 4)`）はそのまま通る（NFR-2, NFR-3）
  - `fake_data.py`: `bars.get(ticker)` を `bar.date >= start` で絞る。未設定 ticker は Err。`"daily_bars_since" in self.errors` なら Err
- 対応 AC: AC-8
- テスト方針（tester）: 偽 `get_stock_bars` が受け取った `StockBarsRequest.start` が `start` の 00:00 UTC であること。負値 / `high < low` のバー → Err（値を含まないメッセージ）。2 回失敗 → 3 回目成功でリトライ（`sleeps == [0.5, 1]`）。3 回失敗で Err。`FakeMarketData` が `start` 以前のバーを落とし、`failing(["daily_bars_since"])` で Err。既存 `test_alpaca_data.py` が無変更で通る
- 依存: なし
- 並列可: T-1, T-3, T-11

### T-3: `engine/execution.py` の切り出し

- 目的: 承認 → 発注 → 約定処理を `run_cycle` と `close_position` の**唯一の共有経路**にする（LR-17）。振る舞いは変えない
- ファイル: `src/trader/engine/execution.py`, `src/trader/engine/cycle.py`, `tests/unit/engine/test_shared_execution_path.py`
- 内容:
  - `execution.py`: `ExecutionDeps(mode, broker, clock, sleep, poll_timeout_s, poll_interval_s)` frozen。`DecisionExecution(decision_id, order: Order | None, poll: PollOutcome | None, error: str | None)`、`ExecutionSummary(orders: int, fills: int, results: tuple[DecisionExecution, ...])`。`execute_decisions(deps, conn, decisions, exit_reasons, market_clock) -> ExecutionSummary` は現在の `_execute_passed_decisions` と同じループ（`rule_check=passed` かつ buy/sell のみ、approval Err / exec Err は次へ）だが、decision ごとの結果を `results` に残す（close の出力 LR-22 と live の「承認なし」判定に使う）
  - `cycle.py`: `resolve_approval` / `execute` / `poll_and_settle` の import と `_execute_passed_decisions` を削除。3 箇所の呼び出しを `execute_decisions(ExecutionDeps(...from deps), conn, ..., ...)` に置き換え、`summary.orders` / `summary.fills` を使う。`_acquire_lock` → `acquire_lock`、`_release_lock` → `release_lock`（公開。T-6 が使う）
  - `test_shared_execution_path.py`: `test_no_bypass.py` と同じ AST 走査で、`src/trader` 内の `ast.Call` のうち関数名が `resolve_approval` / `poll_and_settle`（Name または Attribute）か、`order_executor.execute` / `from trader.engine.order_executor import execute` 由来の `execute` 呼び出しが `engine/execution.py` 以外に無いことを確認する。定義（`def`）は数えない。自己検証（植え込んだ偽ファイルを検出する）も `test_no_bypass.py` に倣って入れる
- 対応 AC: AC-14（静的検査側）, AC-15（リグレッション: `test_cycle.py` / `test_kill_switch.py` / `test_candidates.py` が無変更で通る）
- テスト方針（tester）: 静的走査 + 「`run_cycle` が FakeBroker に対して従来と同じ件数の order / fill を出す」は既存 `test_cycle.py` で担保されるので、新規は静的検査と `ExecutionSummary.results` の内容（approval Err の decision は `order=None, error≠None`）の 2 点
- 依存: なし
- 並列可: T-1, T-2, T-11

### T-4: SPY 記録（`engine/benchmark.py` + cycle 組み込み）

- 目的: `run-cycle` が毎サイクル SPY 日足を起点日から `benchmark_prices` に upsert し、失敗してもサイクルを止めない（LR-13）
- ファイル: `src/trader/engine/benchmark.py`, `src/trader/engine/cycle.py`, `tests/unit/engine/test_benchmark.py`
- 内容:
  - `benchmark.py`: `benchmark_start(conn, now)`: `list_equity_snapshots` の最古 `snapshot_date`（ISO → `date`）、無ければ `loss_limits.trading_day(now)`。`record_benchmark(conn, market, start, now) -> Result[BenchmarkOutcome(recorded, skipped_nonpositive), BenchmarkError]`: `market.daily_bars_since("SPY", start)` → `close.amount > 0` のバーだけ `upsert_benchmark_price(conn, "SPY", bar.date.isoformat(), bar.close, now)`、≤ 0 は `logging.warning` + 件数。市場データ Err / DB Err → `Err(BenchmarkError(message))`
  - `cycle.py`: `market_clock` 成功直後（holdings の前）に `record_benchmark(conn, deps.market, benchmark_start(conn, now), now)` を呼び、Err なら `benchmark_error = message`。`CycleOutcome.benchmark_error: str | None = None` を追加し、`_finish` に既定 `None` の引数で通す（既存呼び出しを壊さない）。halted 分岐は `market_clock` を取らないので記録しない（spec データフロー どおり）
  - `cli.py` の `run-cycle` 出力に `  benchmark: <error>` を 1 行足すのは **T-10 で行う**（本タスクは `cli.py` を触らない）
- 対応 AC: AC-9
- テスト方針（tester）: `FakeMarketData.with_bars("SPY", ...)` に起点前 / 起点以後 / `close=0` のバーを混ぜ、`run_cycle` 後に `benchmark_prices` の行が起点以後かつ正の終値だけ、`close=0` は無い。`failing(["daily_bars_since"])` でも `outcome == "ok"`、`orders` / `fills` は従来どおり、`benchmark_error` に文言。2 回 `run_cycle` しても行数が増えない（上書き）。`equity_snapshots` が空なら当日から、あれば最古日から要求されること（Fake に渡った `start` を bars の内容で間接確認するか、`benchmark_start` を直接テスト）
- 依存: T-1, T-2（T-3 の後の `cycle.py` に乗せる）
- 並列可: T-5, T-7

### T-5: position_marks（holdings upsert / fills delete）

- 目的: 保有銘柄の時価を日次で残し、閉じた日の行は消す（LR-14。条件 2・3 の `unreal(E)` と銘柄別 `PnL_t` の材料）
- ファイル: `src/trader/engine/holdings.py`, `src/trader/engine/fills.py`, `tests/unit/engine/test_marks.py`
- 内容:
  - `holdings.py`: `upsert_position` の直後に `PositionMark(mark_date=trading_day(now).isoformat(), ticker, qty=position.qty, avg_cost=position.avg_cost, mark_price=price, unrealized_pnl=Money((price − avg_cost) × qty), taken_at=now)` を `upsert_position_mark`。Err は `HoldingsError`（既存 `upsert_position` 失敗と同じ扱い = サイクル `error`）。価格 Err の銘柄は mark も書かない
  - `fills.py` `_settle_fill`: `ClosedTrade` 分岐の `delete_position(conn, ticker)` の直後に `delete_position_mark(conn, trading_day(fill.filled_at).isoformat(), ticker)`。部分売り（`OpenPosition`）では消さない（次サイクルの holdings が新しい qty で上書きする）
- 対応 AC: AC-10
- テスト方針（tester）: 保有 2 銘柄・片方だけ価格 Err → mark は 1 行。`unrealized_pnl == (mark − avg_cost) × qty`（cent 量子化）。同日 2 回で上書き（行数不変、`mark_price` 更新）。`poll_and_settle` で全株売り → 当日の mark 行が消え、別日の行は残る。部分売りでは残る
- 依存: T-1
- 並列可: T-4, T-7

### T-6: `engine/manual_close.py`

- 目的: kyo が指定した保有銘柄を、`run_cycle` と同じ経路で全株売る（LR-15〜LR-22）
- ファイル: `src/trader/engine/manual_close.py`, `tests/unit/engine/test_manual_close.py`
- 内容:
  - `CloseDeps(mode, rules, rule_set_sha256, conn, broker, market, clock, poll_timeout_s=60, poll_interval_s=2, sleep=time.sleep, lock_path: Path | None = None)` frozen。`CloseError(kind: Literal["precondition", "approval_required", "order_failed", "ledger"], message)`。`CloseOutcome(cycle_id, decision_id, order_id, order_status, filled_qty, avg_fill_price: Price | None, trade: Trade | None, slot_freed: bool)`
  - `close_position(deps, ticker, reason: str | None)`:
    1. `lock_path` があれば `cycle.acquire_lock`（取れなければ Err「別のサイクルが実行中です」）
    2. 前提検証（**何も書かない**）: (a) `get_position(conn, ticker)` が None → 「`TICKER` を保有していません」 (b) `is_halted` → 「エンジンが halted です（trader resume を先に実行してください）」 (c) `market.market_clock()` Err または `is_open=False` → 「市場時間外です（次の open: …）」 (d) `market.latest_price(ticker)` Err → 「`TICKER` の価格を取得できません」。メッセージにキー値・パス・スタックトレースを含めない
    3. `insert_cycle(conn, f"close_{uuid4().hex}", now, mode)`
    4. decision の再利用: `list_decisions(conn)` から `origin=manual, action=sell, ticker 一致, trading_day(decided_at) == trading_day(now)` かつ `get_order(conn, f"order_{decision.id}") is None`（未発注）の最新 1 件があればそれを使う。無ければ LR-16 のフィールドで新規 `Decision`（`rationale = "manual close by kyo: <reason>"` / 省略時 `"manual close by kyo"`、`proposed_notional = Money(price × qty)`、`reference_price = price`、`rule_check_reason = "manual close (sell of held position; entry rules do not apply)"`、`llm_model/prompt_sha256/response_sha256 = None`）を `insert_decision`
    5. `execute_decisions(ExecutionDeps(from deps), conn, (decision,), {decision.id: ExitReason.manual}, market_clock)`。`exit_reasons` に `manual` を渡すのは、`trade_closer._apply_sell` が「rule exit_reason か LLM origin」以外の閉じ売りを Err にするため（`trade_closer.py` は触らない）
    6. 結果の読み取り（`summary.results[0]`）: `order is None` かつ live → `finish_cycle(outcome="ok", error_summary="approval required")` + `conn.commit()` → `Err(kind="approval_required")`。メッセージに decision id と `trader approve <id> --expires-at <ISO> && trader close TICKER` を含める。`order is None` かつ paper、または `order.status == failed` → `kind="order_failed"`（理由は `error`）。それ以外は `list_trades` から `exit_decision_ids` に decision id を含む trade を探して `CloseOutcome` を組む（部分約定 / タイムアウトは `trade=None`、`order_status` に `partially_filled` / `submitted` を入れる）
    7. `finish_cycle(outcome="ok" | "error")` → `conn.commit()`（`run_cycle._finish` と同じく、別プロセスの `report` / `status` が見えるように）→ lock 解放
  - `engine_state.last_cycle_id` は更新しない（`run_cycle` 専用の鍵。`status` の「last cycle」は `cycles` を `started_at` 順に読むので close 行は出る）
- 対応 AC: AC-11, AC-12, AC-13, AC-14（経路側）
- テスト方針（tester）: **テスト名に `paper_happy_path` / `refuses` / `live` を含める**（verify が `-k` 部分一致。リスク 2）。
  - `test_paper_happy_path_*`: `_ImmediateFillBroker`（`test_cycle.py` の二重を流用）+ `FakeMarketData` + `FixedClock` + 保有 1 銘柄 + 当日 mark 1 行。結果: decision 1 行（`origin=manual, rule_check=passed, action=sell`）、approval 1 行（`approver=system`）、order `filled`、trade 1 行（`exit_reason=manual`、`realized_pnl` が `(fill − avg_cost) × qty`）、`positions` 0 行、`position_marks` 当日行なし、`cycles` に `close_` 行（`outcome` 非 NULL）、`CloseOutcome.slot_freed is True`
  - `test_refuses_*`: 未保有 / halted / `is_open=False` / `latest_price` Err の 4 ケースで Err、`decisions` / `cycles` / `orders` の件数が 0 のまま、メッセージに `/` 始まりのパスや `sk-`・`PK` 等のキー断片が無い
  - `test_live_*`: `mode=live`、承認なし → `broker.submitted == []`、Err に decision id と `trader approve`；同日に再実行 → `decisions` 件数が増えず同じ id；`record_human_approval` で kyo 承認を入れて再実行 → 発注・約定；`expires_at` を `now` より前にした承認では発注されない。`market_clock.next_close` を使う既存 `resolve_approval` の挙動に合わせる
  - 既存 `test_no_bypass.py`（`submit_market_order` は `order_executor.py` のみ）と T-3 の静的検査が通ること
- 依存: T-1, T-3, T-5
- 並列可: T-8

### T-7: `report/readiness.py`（純粋判定）

- 目的: 条件 1〜5 を `Decimal` だけで判定する純粋関数（LR-1〜LR-8）。DB を知らない
- ファイル: `src/trader/report/readiness.py`, `tests/unit/report/test_readiness.py`
- 内容:
  - `ReadinessInputs`（frozen）: `start_date: date | None`, `end_date: date | None`, `since_requested: date | None`, `capital: Money`, `realized_start_by_ticker` / `realized_end_by_ticker: Mapping[str, Money]`, `unreal_start_by_ticker: Mapping[str, Money]`（行が無ければ空 = 0）, `unreal_end_by_ticker: Mapping[str, Money] | None`（保有があるのに mark が無ければ `None` = 未判定）, `benchmark_start: BenchmarkPoint(date, close, substituted: bool) | None`, `benchmark_end: ... | None`, `window_fills: tuple[Fill, ...]`, `window_order_count: int`, `cost: CostAssumptions`, `max_weekly_loss_pct: Decimal`, `equity_series: tuple[tuple[date, Money], ...]`（S 以降、昇順）, `kill_switch_events: tuple[tuple[str, str], ...]`（cycle id, error_summary）, `manual_close_count: int`, `missing: tuple[str, ...]`（不足データ名。表示用）
  - `Verdict(StrEnum)`: `met` / `unmet` / `undetermined` / `needs_review`。`ConditionResult(number: str, label, verdict, detail)`（`"1"`, `"2"`, `"3"`, `"4a"`, `"4b"`, `"4c"`, `"5"`）。`Readiness(start_date, end_date, elapsed_days, realized_pnl, unrealized_pnl, window_pnl, p_pct, p_ex_pct, p_net_pct, estimated_live_pnl, benchmark_pct, benchmark_start, benchmark_end, dd_cap_pct, kill_switch_count, manual_close_count, conditions, missing)`。数値は `None` 許容（未判定時）
  - `evaluate_readiness(inputs) -> Readiness`: spec「読み出しの定義」の式そのまま。`P_net` は `live_estimate.estimate(paper_pnl=window_pnl, fills, order_count, capital, cost).estimated_live_pnl / capital × 100`。`DD_cap = max((running_peak − equity) / capital × 100)`。% は `quantize(Decimal("0.01"), ROUND_HALF_EVEN)` してから比較。判定表は spec の表どおり（条件 2: `P − B ≥ 1.00`、3: `P_ex ≥ B`、4a: `estimated_live_pnl > 0`、4b: `DD_cap < max_weekly_loss_pct`、4c: `kills == 0` else `needs_review`、5: `manual ≥ 1`、1: `(E − S).days ≥ 42`）
  - `float` という語を**コメント・docstring 含め**一切書かない（AC-4 の grep は `\bfloat\b`）
- 対応 AC: AC-16（AC-4 の `readiness.py` 側）
- テスト方針（tester）: AC-16 の列挙を 1 テスト 1 項目で: 41 日 / 42 日境界、`P − B` 0.99 / 1.00、最大 1 銘柄除外（正の最大だけ、全銘柄マイナスなら除かない、2 銘柄が同額正なら 1 つだけ）、`P_net` が `live_estimate` と一致、DD の分母が capital（equity 100,000 / capital 2,500 で $50 下落 → 2.00%）、kills ≥ 1 → `needs_review` と件数、manual 0 → `unmet`、S 無し / B 無し / `unreal_end=None` → `undetermined`。入力が全て `Decimal` 由来であること（`float` を渡さない）
- 依存: T-1（`ExitReason.manual` は使わないが `Fill` / `Money` 型のみ。実質独立）
- 並列可: T-4, T-5

### T-8: `report/readiness_inputs.py`（DB 読み出し）

- 目的: ledger から `ReadinessInputs` を組む（LR-2〜LR-7）。ネットワークに出ない（NFR-6）
- ファイル: `src/trader/report/readiness_inputs.py`, `tests/unit/report/test_readiness_inputs.py`
- 内容: `load_readiness_inputs(conn, rule_set, since: date | None) -> Result[ReadinessInputs, ReadinessInputsError]`
  - `S` = `since` or `min(snapshot_date)`（snapshot 無しなら `start_date=None`、`missing` に `"equity_snapshots"`）。`since < min(snapshot_date)` → `Err("--since は paper 開始日 YYYY-MM-DD 以降を指定してください")`
  - `E` = `max(snapshot_date)`
  - `realized_*_by_ticker`: `list_trades` を `loss_limits.trading_day(closed_at) <= S` / `<= E` で銘柄別合計
  - `unreal_start_by_ticker` = `list_position_marks(S)`（無ければ空）。`unreal_end_by_ticker` = `list_position_marks(E)`；`list_positions` が非空なのに mark が無ければ `None` + `missing` に `"position_marks"`
  - SPY: `list_benchmark_prices("SPY")` から `S` の行（無ければ `S..S+5` の最初、`substituted=True`）と `E` の行（無ければ `E−5..E` の最後、`substituted=True`）。範囲内に無ければ `None` + `missing` に `"benchmark_prices"`
  - `window_fills` = `list_all_fills` のうち `S < trading_day(filled_at) <= E`、`window_order_count` = `list_orders` のうち `submitted_at` が同範囲
  - `equity_series` = snapshots の `snapshot_date >= S`
  - `kill_switch_events` = `list_halted_cycles` のうち `trading_day(started_at) >= S`
  - `manual_close_count` = `exit_reason == manual` の trade のうち `get_decision(exit_decision_ids[-1]).mode == "paper"`
- 対応 AC: AC-17
- テスト方針（tester）: 一時 DB に snapshots（3 日分）/ trades（S 以前・期間内・銘柄 2 つ）/ marks（S 日と E 日）/ benchmark（S 翌営業日からしかない → 代用フラグ、E 当日あり）/ fills・orders（範囲外 1 件を含む）/ cycles（`halted` + `engine is halted` の行と損失上限の行）/ manual trade（exit decision `mode=paper` と `mode=live` の 2 件 → 1 件だけ数える）を入れ、各フィールドを検証。`--since` が S より前で Err。snapshot 無しで `start_date=None` かつ `missing` に名前
- 依存: T-1, T-7
- 並列可: T-6

### T-9: render + `report --since`

- 目的: kyo が `trader report` で移行条件を読める（LR-9〜LR-11、Q6）
- ファイル: `src/trader/report/render.py`, `src/trader/report/__init__.py`, `src/trader/cli.py`（`report` のみ）, `tests/unit/report/test_render_readiness.py`, `tests/unit/test_cli.py`（`report` 関連のみ）
- 内容:
  - `render_readiness(readiness) -> list[str]`: 見出し `## 移行条件（paper → 少額実弾）`、`起点日 / 評価日 / 経過日数`、`ポートフォリオ損益率 P: x.xx%（実現 a / 評価 b / capital c）`、`コスト控除後 P_net: x.xx%`、`SPY リターン B: x.xx%（close(S) → close(E)、配当含まず[、代用: …]）`、条件 1〜5 の各行 `条件 n: <満たした|未達|未判定|要確認> — <根拠数値>`、総合行 `未達 n 件 (…) / 未判定 n 件 (…) / 要確認 n 件 (…)。最終判断は kyo`。未判定の行には不足データ名と「`run-cycle` が 1 回走れば記録されます」。**「合格」「live に切り替え」という文字列を書かない**
  - `render_report(..., readiness: Readiness | None = None)`: 末尾にセクション追加。`None` ならセクションを出さない（既存テストの期待出力を変えない。AC-19）
  - `cli.py report`: `--since` オプション（`YYYY-MM-DD`、不正は exit 1 で `--since must be YYYY-MM-DD`）。`load_readiness_inputs` Err（S より前）は exit 1。`evaluate_readiness` → `render_report(..., readiness=...)`。`--from/--to` は移行条件に影響しない。データ不足でも exit 0
- 対応 AC: AC-18, AC-19, AC-20（`report --since` 側）
- テスト方針（tester）: `test_render_readiness.py`: 満たした / 未達 / 未判定 / 要確認 を 1 つずつ含む `Readiness` を組んで、見出し・`P` / `P_net` / `B` の 3 行・条件 7 行（1, 2, 3, 4a, 4b, 4c, 5）に 4 値のいずれかと数値・総合行の「最終判断は kyo」、`"合格"` と `"live に切り替え"` が無いこと。データ不足版で不足名と `run-cycle` の案内。`test_cli.py`: snapshot が無い DB で `report` exit 0 かつ「移行条件」と「未判定」を含む、`--since not-a-date` で exit 1 と `YYYY-MM-DD`。既存の `test_render.py` / `test_metrics.py` / `test_live_estimate.py` は無変更で通る
- 依存: T-8
- 並列可: なし（`cli.py`）

### T-10: CLI `close` + 依存組み立ての共通化

- 目的: `trader close TICKER` をルーティングだけで `cli.py` に足し、run-cycle との重複を消す（LR-15, LR-24, NFR-8）
- ファイル: `src/trader/cli.py`, `tests/unit/test_cli.py`
- 内容:
  - `_TradingContext(settings, rule_set, limits, rule_set_sha256, conn, broker, market)` frozen と `_build_trading_context(command: str) -> _TradingContext`: 現在の `run_cycle` 本体の settings → lock 検証 → rules → sha → DB → broker / market（paper / live 分岐）を移す。失敗は `typer.echo(f"trader {command}: ...", err=True)` + `Exit(1)`、DB を開いた後の失敗では `conn.close()`
  - `run-cycle`: `_build_trading_context("run-cycle")` を使うよう書き換え。出力に `benchmark_error` があれば `  benchmark: <error>` を 1 行
  - `close`: `TICKER` 引数、`--reason TEXT`。`TRADER_ENV=test` では `run-cycle` と同じ文言で exit 1。`CloseDeps` を組んで `close_position`。成功: `decision <id> / order <id> (<status>) / filled <qty> @ <avg> / trade <id> realized <pnl>` と「枠が 1 つ空きました。次の run-cycle で新規買いが走る可能性があります」。部分約定 / タイムアウト: `orders.status` と約定済み株数、「残りは次の run-cycle のポーリング対象になりません（v1 の制限）」。Err: `trader close: <message>` で exit 1（`approval_required` は案内文をそのまま）
  - `cli.py` に `resolve_approval` / `execute_decisions` / `poll_and_settle` の文字列を**コメントにも**書かない（AC-23）
  - 行数: 作業後に `wc -l src/trader/cli.py` を commit メッセージに書く（リスク 1）
- 対応 AC: AC-20, AC-23, AC-24 以外の CLI 系, AC-9（CLI 表示側）
- テスト方針（tester）: `COMMANDS` に `["close", "--help"]`。`runner.invoke(app, ["close", "ABCD"])` が exit 1 で「テスト環境」。`_build_trading_context` は `run-cycle` と同じく test 環境では実行されないので、settings 不足の人間向けエラーは既存の `run-cycle` テストに準ずる（追加しない）。`for c in ...; do uv run trader $c --help; done` が通る
- 依存: T-6, T-9
- 並列可: なし（`cli.py`）

### T-11: 親 spec Q5 の更新

- 目的: 親 spec の移行条件が本 spec を指す（LR-23）
- ファイル: `specs/ai-auto-trader/spec.md`
- 内容: 477 行目（`- Q5: 段階移行条件 → ...` で始まる行）を **1 行のまま** 次の形にする: `- Q5: 段階移行条件 → **2026-10-06 改訂**: 移行条件は `specs/live-readiness/spec.md` を正とする（『閉じた trade 10 件以上』は廃止。旧記述は履歴として残す）。~~旧: 方向性を確定（実装は別 spec）: paper → 少額実弾は「paper 3 週間以上 + 閉じた trade 10 件以上 + 実弾期待値がプラス + 最大 DD が weekly 上限未満 + キルスイッチが誤作動していない」。的中率の確定を待たない~~`。他の行は触らない。親 spec の承認欄も触らない
- 対応 AC: AC-24
- テスト方針: verify の `grep` / `awk` がそのまま検査（`^- Q5:` の最初の行に `2026-10-06` が含まれる）
- 依存: なし
- 並列可: T-1, T-2, T-3。commit は `docs(spec): ...`

### T-12: 統合テスト + カバレッジ + 全 AC 掃討

- 目的: FakeBroker + 実 SQLite で「買い → SPY / mark 記録 → 手動 close → report に条件 5 満たした → 空いた枠で次の買い」を一気通貫で証明し、全 verify を通す
- ファイル: `tests/integration/test_manual_close_cycle.py`, 不足分の `tests/unit/**`
- 内容（`test_paper_cycle.py` の `_setup_workspace` / `_ImmediateFillBroker` / `_FakeLlm` を流用）:
  1. `rules approve` → `ingest`（CLI）
  2. `run_cycle`（AAPL $10、市場 open、SPY bars 付き `FakeMarketData`、`FixedClock` day 1）→ 7 株 buy filled。`benchmark_prices` に SPY 行、`position_marks` に AAPL 行（day 1）
  3. `run_cycle`（`FixedClock` day 2、AAPL $11）→ mark が day 2 に増え、`benchmark_prices` が day 2 まで埋まる
  4. `close_position`（day 2、$11 で filled）→ trade 1 件 `exit_reason=manual`、`positions` 空、day 2 の mark が消えている、`cycles` に `close_` 行
  5. `trader report`（CLI）→ 「移行条件」セクションがあり、`条件 5: 満たした`、`SPY リターン` と `ポートフォリオ損益率` の行、`manual: 1` が「ルール発火回数」に出る、「合格」が無い
  6. `run_cycle`（day 3）→ AAPL を再び buy（`orders == 1`、`positions` 1 行）= 空いた枠で新規買い（Q7）
  - 最後に spec の全 verify（AC-1〜AC-24）を実行し、失敗があれば該当タスクに戻す。カバレッジ不足は `# pragma: no cover` で逃げずテストを足す
- 対応 AC: AC-3, AC-21, AC-22（+ 全 AC の最終確認）
- テスト方針（tester）: 上記シナリオ。`FixedClock` を日ごとに `advance` して `snapshot_date` / `mark_date` / `price_date` が別日になること（同一日だと条件 1 の経過日数が 0 で読みにくい）
- 依存: T-4, T-10（= 全タスク）
- 並列可: なし

## リスク

spec に根拠があるものだけを挙げる。「未確認」は隠さない。

1. **`cli.py` の行数（NFR-8 / AC-23 の 800 行上限）**: 現在 649 行。`close` のルーティング（約 60 行）、`report --since` + readiness 呼び出し（約 30 行）、`run-cycle` 出力 1 行を足し、`_build_trading_context` への共通化で `run-cycle` が約 60 行縮む見込み。概算 700〜740 行で上限内だが、builder は T-10 完了時に `wc -l` を commit メッセージに書くこと。**780 行を超える場合**の退避先: `report` コマンド本体の集計（metrics / estimate / readiness の組み立て、約 60 行）を `src/trader/report/assemble.py` の `build_report(conn, rule_set, period, since) -> Result[str, str]` に移し、`cli.py` は呼ぶだけにする（NFR-8 の「cli.py はルーティングと依存組み立てだけ」に沿う）。reviewer はこの退避を仕様違反ではなく許容として扱う
2. **AC-11〜AC-13 の `-k` 部分一致**: `-k "live"` は `delivers` 等の部分文字列にも当たり、`-k "refuses"` と `-k "paper_happy_path"` も同様。tester は `test_manual_close.py` の関数名を `test_paper_happy_path_*` / `test_refuses_*` / `test_live_*` の 3 接頭辞に限定し、`live` を他の名前に含めない（例: `deliver` を使わない）。3 つの選択がそれぞれ空集合にならないことを `pytest --collect-only -k` で確認する
3. **AC-4 / AC-23 の grep はコメント・docstring にも効く**: `readiness.py` に `float` という語（「float を使わない」という注記も含む）を書かない。`cli.py` に `resolve_approval` / `execute_decisions` / `poll_and_settle` を書かない（`close_position` の docstring で経路を説明したくなる箇所に注意）
4. **Alpaca の `StockBarsRequest.start` と日付境界**: `daily_bars_since` は `start` を 00:00 UTC の `datetime` にして渡す。Alpaca の日足 `t` は 04:00Z / 05:00Z（ET 深夜）なので `_to_bar` の `timestamp.date()` は ET 日と一致し、`start` 当日のバーも含まれる。これは既存 `daily_bars` と同じ前提で、実 API では未検証（T-8 の親 plan と同じ扱い）。AC-25 の kyo 確認で `benchmark_prices.price_date` が営業日になっていることを見る
5. **当日分の SPY バーは暫定値**: 市場時間内のサイクルで取る当日バーは「ここまでの終値」で、翌日以降のサイクルで上書きされる（spec データモデル備考）。`report` の `B` は評価日当日の暫定終値を使うことになるが、spec どおり。表示上は区別しない
6. **本番 DB に `position_marks` が無い期間**: paper は 2026-09-18 頃から回っているが、mark は本 spec の deploy 後の最初の `run-cycle` から記録される。既定の `S`（最古 snapshot 日）では `unreal(S) = 0` が正しい（保有ゼロ）ので条件 2・3 は deploy 後 1 サイクルで判定可能。`--since` を deploy 前の日付にすると「未判定」になる（spec D-1 の注記どおり。バグではない）
7. **`manual` 件数の「exit decision の mode」**: `trades.exit_decision_ids` は JSON 配列で、最後の id の decision を `get_decision` で引いて `mode` を見る。close は全株売りなので exit decision は常に 1 件だが、将来 `--qty` を足した場合は最後の id で判定する前提を `readiness_inputs.py` の docstring に書く
8. **close と cron の同時実行**: `close` は `run-cycle` と同じ `var/run-cycle.lock` を取る。cron の `run-cycle` が走っている最中に `close` を打つと「別のサイクルが実行中です」で exit 1 になり、kyo が数十秒後に再実行する運用になる（spec N-10 のとおり。D-6「寄り付き直後を避ける」の推奨と合わせて AC-26 の手順に書く）
9. **live の承認経路は unit テストでしか通らない**: `TRADER_MODE=live` での `close` は spec スコープ外（次の intent）。T-6 の `test_live_*` が R-17 の非迂回を担保する唯一の証拠
10. **`render_report` の引数追加**: AC-19 は既存 `test_render.py` を無変更で通すことを要求する。`readiness` は末尾のキーワード引数（既定 `None`）にし、`None` のときは出力を 1 文字も変えない
11. **`CycleOutcome` / `_finish` の引数追加**: `benchmark_error` を既定 `None` で足す。`_finish` の 10 箇所近い呼び出しを全て書き換えず、キーワードで 1 箇所だけ渡す（AC-15 のリグレッションを避ける）
12. **Alpaca paper 口座の現金残高（kyo 確認事項 4、未確認）**: `DD_cap` と `P` の分母は `capital_usd`（$2,500）なので口座残高が $100k でも % は正しく出る。一方、既存のキルスイッチ DD 判定（peak 比）が実質効いていない可能性は本 spec では扱わない（スコープ外）。AC-25 の確認時に `equity_snapshots.cash` を kyo が見て、別 intent にするか決める
13. **条件 1 の最短到達日**: 最古 snapshot が 2026-09-18 なら 42 日後は 2026-10-30。本 spec の deploy 直後は条件 1 が「未達」で表示されるのが正常

## Proof

「この計画が完了した」= spec の全 verify が exit 0（Stop hook が判定）+ AC-25 / AC-26 を kyo が Deploy 後に確認。

| 受入基準 | 証明方法 | 対応タスク |
|---|---|---|
| AC-1 | `uv run ruff check . && uv run ruff format --check .` | 全タスクで維持 |
| AC-2 | `uv run mypy src` | 全タスクで維持 |
| AC-3 | `uv run pytest tests/unit -q --cov=src --cov-fail-under=80` | T-12（各タスクが積み上げ） |
| AC-4 | `scripts/check_no_float_money.py` + `readiness.py` に `float` の語が無い | T-1（ledger 側）, T-7 |
| AC-5 | `tests/unit/domain/test_manual_origin.py` | T-1 |
| AC-6 | `tests/unit/ledger/test_migration_0002.py`（0001 のみ適用済み DB への無停止適用） | T-1 |
| AC-7 | `tests/unit/ledger/test_benchmark_repository.py` + 既存 `test_sql_safety.py` | T-1 |
| AC-8 | `tests/unit/market/test_daily_bars_since.py` + 既存 `test_alpaca_data.py` | T-2 |
| AC-9 | `tests/unit/engine/test_benchmark.py` | T-4 |
| AC-10 | `tests/unit/engine/test_marks.py` | T-5 |
| AC-11 | `tests/unit/engine/test_manual_close.py -k paper_happy_path` | T-6 |
| AC-12 | `tests/unit/engine/test_manual_close.py -k refuses` | T-6 |
| AC-13 | `tests/unit/engine/test_manual_close.py -k live` | T-6 |
| AC-14 | `tests/unit/engine/test_shared_execution_path.py`（AST 走査）+ 既存 `test_no_bypass.py` | T-3, T-6 |
| AC-15 | 既存 `test_cycle.py` / `test_kill_switch.py` / `test_candidates.py` が無変更で通る | T-3, T-4, T-5 |
| AC-16 | `tests/unit/report/test_readiness.py` | T-7 |
| AC-17 | `tests/unit/report/test_readiness_inputs.py` | T-8 |
| AC-18 | `tests/unit/report/test_render_readiness.py` | T-9 |
| AC-19 | 既存 `test_metrics.py` / `test_live_estimate.py` / `test_render.py` が無変更で通る | T-9 |
| AC-20 | 8 コマンドの `--help` + `tests/unit/test_cli.py`（close の test 環境拒否、`--since` 不正） | T-9, T-10 |
| AC-21 | `tests/integration/test_manual_close_cycle.py` | T-12 |
| AC-22 | 既存 `tests/integration/test_paper_cycle.py` が無変更で通る | T-12 |
| AC-23 | `find src ... NR>800` + `cli.py` に 3 識別子が無い | T-10（リスク 1） |
| AC-24 | `grep` / `awk` で親 spec Q5 行 | T-11 |
| AC-25 | **人間確認（Deploy 後）**: 市場時間内の `run-cycle` 1 回後に `trader report` の移行条件セクションに SPY リターンと保有 5 銘柄込みの損益率が出る。あわせて `benchmark_prices.price_date` が営業日であること（リスク 4）と `equity_snapshots.cash`（リスク 12）を見る | – |
| AC-26 | **人間確認（Deploy 後）**: paper で `trader close <ticker>` を 1 回（cron と重ならない時間帯、寄り付き直後を避ける。リスク 8）→ `trader report` に `manual` の trade 1 件と `条件 5: 満たした` → `trader status` で次サイクルの新規買い | – |

対応の無い AC: なし。

## 承認

approved_by:
approved_at:
