# Plan: ai-auto-trader

元 spec: `specs/ai-auto-trader/spec.md`（approved_at: 2026-09-14）
元 intent: `specs/ai-auto-trader/intent.md`（approved_at: 2026-09-14）
作成日: 2026-09-14
状態: draft

> **kyo へ（これが自律ループ前の最後の介入点）**
> - リポジトリには現在 `pyproject.toml` / `src/` / `tests/` が**存在しない**。T-1 で spec の推奨スタック（Python 3.12 + uv）を初期化する。
> - タスクは 15 個（spec の典型目安 5〜12 を超える）。理由: spec が「paper 運用の完成 + live 承認フローの土台」まで含み、設計の構成図に約 40 の新規ソースがあるため。分割はせず、依存の薄いタスクを並列グループにまとめて対処する。
> - spec の構成図に無いファイルを 6 つ追加した（`domain/retry.py`, `engine/holdings.py`, `engine/candidates.py`, `ledger/source_repository.py`, `ledger/portfolio_repository.py`, `tests/fixtures/*`）。いずれも N-4（リトライ共通化）と N-8（1 ファイル 400 行目安）のための分割で、機能追加ではない。「変更ファイル」の備考に理由を書いた。不要なら削ってよい。
> - `rules/trading-rules.lock` は本 plan では**生成しない**。`trader rules approve` は kyo が `approved_by` を記入して Deploy 後に実行する（AC-32 の手順に含める）。

## 変更ファイル

すべて新規（既存のソースコードは無い）。`変更` は既存の設定ファイルのみ。

### ルート

| ファイル | 種別 | 目的 |
|---|---|---|
| `pyproject.toml` | 新規 | uv プロジェクト定義。依存（下記「依存バージョン」）、`[project.scripts] trader = "trader.cli:app"`、ruff / mypy(strict, pydantic plugin) / pytest / coverage 設定、`ledger/migrations/*.sql` をパッケージデータに含める設定 |
| `uv.lock` | 新規 | `uv sync` が生成。依存の正確なバージョンを固定（spec「バージョンは plan で固定」の実体） |
| `.env.example` | 新規 | N-2 の変数名を値なしで列挙（AC-30 の 6 変数 + `ALPACA_*_SECRET` 4 つ + `ALPACA_LIVE_READ_KEY` + `ANTHROPIC_MODEL` + `TRADER_ENV`） |
| `.gitignore` | 変更 | `data/`, `var/`, `.coverage`, `htmlcov/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/` を追加（既存行は保持） |
| `rules/trading-rules.yaml` | 新規 | spec「ルール定義」の初期値をそのまま（`approved_by: ""` のまま。kyo が承認時に記入） |
| `scripts/check_no_float_money.py` | 新規 | AST 走査で `src/trader/{domain,broker,ledger,rules}` の `float` 注釈・`float()` 呼び出し・`float` 戻り値を検出し、file:line を出して exit 1（AC-23） |

### `src/trader/`

| ファイル | 種別 | 目的 |
|---|---|---|
| `src/trader/__init__.py` | 新規 | パッケージ。バージョン文字列のみ |
| `src/trader/cli.py` | 新規 | typer エントリ。`ingest / run-cycle / approve / report / rules approve / resume / status` の薄いルーティング。設定の読み込みは各コマンド本体の中で行う（`--help` は環境変数なしで exit 0） |
| `src/trader/config/__init__.py` | 新規 | |
| `src/trader/config/mode.py` | 新規 | `TradingMode` enum、`decide_mode(...) -> Result[TradingMode, ConfigError]` 純粋関数（R-15） |
| `src/trader/config/settings.py` | 新規 | pydantic-settings。`PaperSettings` / `LiveSettings` を別クラスにし、モードに応じて片方だけロード（N-2, N-3）。`SecretStr` |
| `src/trader/domain/__init__.py` | 新規 | |
| `src/trader/domain/result.py` | 新規 | `Ok[T]` / `Err[E]` frozen dataclass + `Result` 型エイリアス（N-6） |
| `src/trader/domain/money.py` | 新規 | `Money`（USD セント量子化）、`Price`、`Quantity`（整数株）、`pct_of(capital, pct) -> Money`、canonical 文字列変換（N-1） |
| `src/trader/domain/clock.py` | 新規 | `Clock` Protocol、`SystemClock`、`FixedClock`（N-9） |
| `src/trader/domain/models.py` | 新規 | `Decision, Approval, Order, Fill, Position, Trade, Evidence, MentionStats, EquitySnapshot, ExitSignal, RuleCheck` と状態 enum。全て frozen（N-7） |
| `src/trader/domain/retry.py` | 新規 | **spec 構成に無い追加**。`RetryPolicy`（最大回数・基本待ち・上限）と指数バックオフの待ち時間列を返す純粋関数。broker / market / analysis が共用（N-4） |
| `src/trader/sources/__init__.py` | 新規 | |
| `src/trader/sources/adapter.py` | 新規 | `SourceAdapter` Protocol（`source_id`, `load(path) -> Result[tuple[Mention, ...], IngestError]`）と `AdapterRegistry`（frozen。登録は新しいレジストリを返す）（R-3） |
| `src/trader/sources/serenity/__init__.py` | 新規 | |
| `src/trader/sources/serenity/schema.py` | 新規 | tweets.json の pydantic スキーマ（`id` 数値文字列、`created_at`/`timestamp`、`text`、`url` 任意、未知フィールド無視）と ticker ホワイトリストのパーサ（R-2） |
| `src/trader/sources/serenity/adapter.py` | 新規 | ファイル読み込み → 全件検証（1 件でも不正なら件数付き Err、何も返さない）→ `$TICKER` 抽出 → `Mention` 列（R-1, R-2） |
| `src/trader/sources/evidence.py` | 新規 | mentions → 銘柄別 `MentionStats`（初出・直近・回数・14 日内の増減）と抜粋 → `Evidence`（R-4, Q4） |
| `src/trader/market/__init__.py` | 新規 | |
| `src/trader/market/data_provider.py` | 新規 | `MarketDataProvider` Protocol: `daily_bars(ticker, days)`, `latest_price(ticker)`, `market_clock()`（is_open / next_close / next_open）（Q7） |
| `src/trader/market/alpaca_data.py` | 新規 | alpaca-py `StockHistoricalDataClient` + `TradingClient.get_clock` のラッパ。レスポンスを pydantic 検証、GET リトライ、タイムアウト。SDK クライアントはコンストラクタ注入 |
| `src/trader/market/fake_data.py` | 新規 | テスト用。価格・営業時間を差し替え可能な frozen 実装 |
| `src/trader/rules/__init__.py` | 新規 | |
| `src/trader/rules/schema.py` | 新規 | `RuleSet`（frozen pydantic、`extra="forbid"` を全ネストに適用、pct 0〜100・capital 正の検証）、`DerivedLimits`、`load_rules(path) -> Result`、`derive_limits(rules) -> DerivedLimits`（R-9, R-11） |
| `src/trader/rules/lock.py` | 新規 | SHA-256 計算、`verify_lock(yaml_path, lock_path) -> Result`、`write_lock(...)`（`rules approve` 専用）（R-10） |
| `src/trader/rules/entry_checks.py` | 新規 | 買い前検証 → `Result[EntryPlan, RuleRejection]`。株数 = floor(上限 / 現在値)、1 株が上限超なら拒否。理由文に導出額と比率を併記（R-12） |
| `src/trader/rules/exit_checks.py` | 新規 | 保有中の判定 → `ExitSignal | None`（損切り / 部分利確 / トレーリング / 保有期間）（R-13） |
| `src/trader/rules/loss_limits.py` | 新規 | 日次・週次損失（実現 + 評価）とドローダウン % の集計・判定 → `LossLimitBreach | None`（R-19） |
| `src/trader/analysis/__init__.py` | 新規 | |
| `src/trader/analysis/output_schema.py` | 新規 | LLM 出力の pydantic スキーマ（`extra="forbid"`、ルール変更フィールド無し、ticker はホワイトリスト検証）（R-5, R-7） |
| `src/trader/analysis/prompt.py` | 新規 | evidence + 日足 + ルール（読み取り専用テキスト）からプロンプトを組む純粋関数。**ファイル名 `trading-rules` を文字列として含めない**（AC-10 の grep 対象） |
| `src/trader/analysis/llm_client.py` | 新規 | anthropic 呼び出し。タイムアウト・リトライ、prompt/response の SHA-256。クライアント注入 |
| `src/trader/analysis/analyst.py` | 新規 | evidence + 株価 → `tuple[Proposal, ...]`。検証失敗は `skip` 扱いで返す（R-5） |
| `src/trader/broker/__init__.py` | 新規 | |
| `src/trader/broker/broker.py` | 新規 | `Broker` Protocol（`submit_market_order`, `cancel_all_open`, `get_order`, `positions`, `account`）と `BrokerError` |
| `src/trader/broker/alpaca_broker.py` | 新規 | alpaca-py 実装。モードから URL を決定し他を拒否、trade キー必須、発注 POST リトライなし、`client_order_id` 冪等、レスポンス pydantic 検証（R-14, N-3, N-4） |
| `src/trader/broker/fake_broker.py` | 新規 | テスト用。約定・部分約定・拒否・キャンセルをシナリオで返す |
| `src/trader/ledger/__init__.py` | 新規 | |
| `src/trader/ledger/db.py` | 新規 | sqlite3 接続（`PRAGMA foreign_keys=ON`, `journal_mode=WAL`）、マイグレーション適用、`transaction()` コンテキスト |
| `src/trader/ledger/migrations/0001_init.sql` | 新規 | spec「データモデル」の 11 テーブル |
| `src/trader/ledger/repository.py` | 新規 | decisions / approvals / orders / fills の書き込み・読み出し（パラメータ化のみ）（R-16, R-18, R-20） |
| `src/trader/ledger/portfolio_repository.py` | 新規 | **spec 構成に無い追加（400 行目安のため分割）**。positions / trades / equity_snapshots / engine_state / cycles |
| `src/trader/ledger/source_repository.py` | 新規 | **同上**。sources / mentions（`external_id` で重複排除、ingest 冪等） |
| `src/trader/ledger/trade_closer.py` | 新規 | fills → Position 更新（avg_cost, high_watermark, partial_tp_done）、qty 0 で `Trade` 生成（R-21） |
| `src/trader/engine/__init__.py` | 新規 | |
| `src/trader/engine/approval.py` | 新規 | paper: `approver=system` 自動承認 / live: kyo の有効承認を検索。`expires_at` = 次セッション終了（R-17） |
| `src/trader/engine/order_executor.py` | 新規 | 唯一の発注経路。`ApprovedOrderRequest`（approval.py だけが生成できる型）を受け、decision → approval → order(recorded) を DB に書けた場合のみ `broker.submit`（R-16） |
| `src/trader/engine/kill_switch.py` | 新規 | 未約定全キャンセル → `engine_state.halted=true` + 理由・時刻を永続化 → ログ（R-19）。`resume` の解除処理も持つ |
| `src/trader/engine/holdings.py` | 新規 | **spec 構成に無い追加（cycle.py の分割）**。保有の評価額計算、high_watermark 更新、exit_checks 適用 → `rule_exit` decision（データフロー 2〜3） |
| `src/trader/engine/candidates.py` | 新規 | **同上**。evidence 抽出 → analyst → entry_checks → decision(passed/rejected)（データフロー 4） |
| `src/trader/engine/cycle.py` | 新規 | 1 サイクルのオーケストレーション。起動検証 → halted 判定 → loss_limits → holdings → candidates → approval → executor → 約定ポーリング → cycles 記録。`var/run-cycle.lock` で同時実行防止（R-8, N-10） |
| `src/trader/report/__init__.py` | 新規 | |
| `src/trader/report/metrics.py` | 新規 | 期間損益・勝率・平均損益・最大 DD・ルール発火回数・判断内訳（純粋関数）（R-22） |
| `src/trader/report/live_estimate.py` | 新規 | Q10 の式で実弾期待値（R-23） |
| `src/trader/report/render.py` | 新規 | テキスト整形（AC-33 の読みやすさはここ） |

### `tests/`

| ファイル | 種別 | 目的 |
|---|---|---|
| `tests/conftest.py` | 新規 | `TRADER_ENV=test` を import 時に強制。autouse fixture で `socket.socket.connect` を例外にしてネットワークを封じる（N-9）。共通 fixture（`FixedClock`、一時 DB、一時ルール YAML + lock） |
| `tests/fixtures/serenity/tweets_valid.json` | 新規 | 3〜5 件の正常ツイート（銘柄 2 つ以上） |
| `tests/fixtures/serenity/tweets_invalid.json` | 新規 | 正常 + 不正混在（`id` 欠落、日時不正） |
| `tests/fixtures/serenity/ticker_stats.txt` | 新規 | ホワイトリストのサンプル |
| `tests/fixtures/rules/valid.yaml` | 新規 | spec 初期値 + `approved_by: kyo` |
| `tests/fixtures/rules/invalid_*.yaml` | 新規 | `*_usd` 混入 / pct 範囲外 / capital 非正 |
| `tests/unit/test_cli.py` | 新規 | typer `CliRunner` で各コマンドの `--help` と、環境変数無しでの人間向けエラー |
| `tests/unit/domain/test_immutability.py` | 新規 | AC-26 |
| `tests/unit/domain/test_money.py` | 新規 | 量子化・pct 導出・canonical 文字列 |
| `tests/unit/domain/test_retry.py` | 新規 | バックオフ列 |
| `tests/unit/config/test_mode_guard.py` | 新規 | AC-13 |
| `tests/unit/config/test_key_separation.py` | 新規 | AC-14 |
| `tests/unit/config/test_settings_validation.py` | 新規 | AC-25 |
| `tests/unit/sources/test_serenity_adapter.py` | 新規 | AC-4 |
| `tests/unit/sources/test_adapter_registry.py` | 新規 | AC-5 |
| `tests/unit/sources/test_evidence.py` | 新規 | AC-6 |
| `tests/unit/market/test_alpaca_data.py` | 新規 | レスポンス検証・GET リトライ・タイムアウト（注入した偽 SDK クライアント） |
| `tests/unit/rules/test_lock.py` | 新規 | AC-9 |
| `tests/unit/rules/test_immutable_rules.py` | 新規 | AC-10 |
| `tests/unit/rules/test_derived_limits.py` | 新規 | AC-11 |
| `tests/unit/rules/test_entry_checks.py` | 新規 | AC-11 |
| `tests/unit/rules/test_exit_checks.py` | 新規 | AC-12 |
| `tests/unit/rules/test_loss_limits.py` | 新規 | 日次 / 週次 / DD の集計 |
| `tests/unit/rules/test_schema_validation.py` | 新規 | AC-25 |
| `tests/unit/analysis/test_output_schema.py` | 新規 | AC-7 |
| `tests/unit/analysis/test_prompt.py` | 新規 | ルールが読み取り専用テキストとして入る / 文字列 `trading-rules` を含まない |
| `tests/unit/analysis/test_llm_client.py` | 新規 | AC-24 |
| `tests/unit/broker/test_alpaca_urls.py` | 新規 | AC-14 |
| `tests/unit/broker/test_retry_policy.py` | 新規 | AC-24 |
| `tests/unit/broker/test_response_validation.py` | 新規 | AC-25 |
| `tests/unit/broker/test_fake_broker.py` | 新規 | FakeBroker のシナリオ動作 |
| `tests/unit/ledger/test_db.py` | 新規 | マイグレーション適用・トランザクション巻き戻し |
| `tests/unit/ledger/test_decision_record.py` | 新規 | AC-19 |
| `tests/unit/ledger/test_fills.py` | 新規 | AC-17 |
| `tests/unit/ledger/test_trade_closer.py` | 新規 | AC-20 |
| `tests/unit/engine/test_approval.py` | 新規 | AC-16 |
| `tests/unit/engine/test_record_before_submit.py` | 新規 | AC-15 |
| `tests/unit/engine/test_kill_switch.py` | 新規 | AC-18 |
| `tests/unit/engine/test_no_bypass.py` | 新規 | AC-8 |
| `tests/unit/engine/test_cycle.py` | 新規 | 部分失敗（LLM 失敗 → skip / マーケットデータ失敗 → 買わない / halted → 発注なし / 市場時間外 → 判断のみ） |
| `tests/unit/report/test_metrics.py` | 新規 | AC-21 |
| `tests/unit/report/test_live_estimate.py` | 新規 | AC-22 |
| `tests/unit/report/test_render.py` | 新規 | 出力に根拠・売却理由・損益が含まれる |
| `tests/integration/test_paper_cycle.py` | 新規 | AC-28 |

### 依存バージョン（T-1 で `pyproject.toml` に書く。正確な値は `uv.lock` が固定）

| 種別 | パッケージ | 制約 |
|---|---|---|
| runtime | python | `>=3.12,<3.13` |
| runtime | `pydantic` | `>=2.9,<3` |
| runtime | `pydantic-settings` | `>=2.5,<3` |
| runtime | `alpaca-py` | `>=0.30,<1` |
| runtime | `anthropic` | `>=0.40,<1` |
| runtime | `typer` | `>=0.12,<1` |
| runtime | `pyyaml` | `>=6,<7` |
| dev | `pytest` `pytest-cov` `ruff` `mypy` `types-PyYAML` | 最新の安定版を `uv add --dev` で解決し lock に固定 |

HTTP レベルのモックライブラリ（`respx` 等）は**使わない**。外部 SDK クライアントはコンストラクタ注入にし、テストは偽クライアントを渡す（alpaca-py は `requests`、anthropic は `httpx` で HTTP 層が異なり、2 種のモックを保守するより境界で差し替える方が薄い）。

## 作業順序

1 タスク = 1 commit（tester の RED commit を含めると 2）。各タスク完了時点で `ruff` / `mypy src` / `pytest tests/unit` が通る状態を保つ。CLI は T-1 でスタブ化し、以降のタスクで本体を差し替える（AC-29 を早期に満たし、`cli.py` を薄く保つ）。

### 概要表

| ID | 内容 | 依存 | 並列可 |
|---|---|---|---|
| T-1 | プロジェクト初期化（pyproject / CLI スタブ / conftest / .env.example / .gitignore / float 検査スクリプト） | – | – |
| T-2 | domain（Result / Money / Clock / models / retry） | T-1 | – |
| T-3 | config（mode 判定 / Paper・Live Settings） | T-2 | T-4, T-5 と並列可 |
| T-4 | ledger 基盤（db / migrations / 3 repository） | T-2 | T-3, T-5 と並列可 |
| T-5 | rules 定義（schema / DerivedLimits / lock / `rules approve` / trading-rules.yaml） | T-2 | T-3, T-4 と並列可 |
| T-6 | rules 判定（entry / exit / loss_limits） | T-5 | T-7〜T-11 と並列可 |
| T-7 | sources（adapter registry / serenity / evidence / `ingest`） | T-4 | T-6, T-8〜T-11 と並列可 |
| T-8 | market（provider / alpaca_data / fake_data） | T-3 | T-6, T-7, T-9〜T-11 と並列可 |
| T-9 | broker（protocol / alpaca_broker / fake_broker） | T-3 | T-6〜T-8, T-10, T-11 と並列可 |
| T-10 | analysis（output_schema / prompt / llm_client / analyst） | T-5 | T-6〜T-9, T-11 と並列可 |
| T-11 | ledger trade_closer（fills → positions / trades） | T-4 | T-6〜T-10 と並列可 |
| T-12 | engine 基盤（approval / order_executor / kill_switch / `approve` `resume` `status`） | T-6, T-9, T-11 | T-14 と並列可 |
| T-13 | engine cycle（holdings / candidates / cycle / `run-cycle`） | T-7, T-8, T-10, T-12 | – |
| T-14 | report（metrics / live_estimate / render / `report`） | T-11 | T-12 と並列可 |
| T-15 | 統合テスト + カバレッジ 80% + 全 AC 掃討 | T-13, T-14 | – |

### 並列グループ（`build-test` ワークフローに渡す単位）

| グループ | タスク | 前提 |
|---|---|---|
| G1 | T-1 | – |
| G2 | T-2 | G1 |
| G3 | T-3, T-4, T-5 | G2 |
| G4 | T-6, T-7, T-8, T-9, T-10, T-11 | G3 |
| G5 | T-12, T-14 | G4 |
| G6 | T-13 | G5 |
| G7 | T-15 | G6 |

G4 は 6 タスクを同時に回せるが、`tests/conftest.py` と `src/trader/domain/models.py` は触らない前提（変更が必要なら T-2 の範囲に戻して直列にする）。worktree 分離でマージ衝突を避けるため、各タスクは自分の行のファイルだけを触る。

### T-1: プロジェクト初期化

- 目的: spec のスタックで `uv sync` / lint / typecheck / test が空で通る土台を作り、CLI の全サブコマンドを `--help` が返るスタブとして置く
- ファイル: `pyproject.toml`, `uv.lock`, `.env.example`, `.gitignore`（変更）, `src/trader/__init__.py`, `src/trader/cli.py`, `tests/conftest.py`, `tests/unit/test_cli.py`, `scripts/check_no_float_money.py`
- 内容:
  - `pyproject.toml`: 上記の依存制約。`[tool.ruff]` line-length 100、`select = ["E","F","I","B","UP","N","S","RUF"]`（`tests/` は `S101` 除外）。`[tool.mypy]` `strict = true`, `plugins = ["pydantic.mypy"]`, `mypy_path = "src"`。alpaca-py の型情報が不足していれば `[[tool.mypy.overrides]] module = "alpaca.*"` で `ignore_missing_imports` を許可し、境界で pydantic 検証する（リスク参照）。`[tool.pytest.ini_options]` `testpaths = ["tests"]`。`[tool.coverage.run] source = ["src"]`
  - `cli.py`: typer アプリ + `rules` サブアプリ。各コマンドは `NotImplemented` の人間向けメッセージで exit 1（`--help` は exit 0）。設定読み込みはコマンド関数の内部で行う（モジュール import では環境変数を読まない）
  - `conftest.py`: モジュール先頭で `os.environ["TRADER_ENV"] = "test"`。autouse fixture でソケット接続を `RuntimeError("network disabled in tests")` にする
  - `check_no_float_money.py`: `ast` で `Name(id="float")` を注釈・戻り値・`Call.func` の位置で検出。対象ディレクトリは AC-23 の 4 つ。違反なしで exit 0
  - `.env.example`: `ALPACA_PAPER_READ_KEY=` `ALPACA_PAPER_READ_SECRET=` `ALPACA_PAPER_TRADE_KEY=` `ALPACA_PAPER_TRADE_SECRET=` `ALPACA_LIVE_READ_KEY=` `ALPACA_LIVE_READ_SECRET=` `ALPACA_LIVE_TRADE_KEY=` `ALPACA_LIVE_TRADE_SECRET=` `ANTHROPIC_API_KEY=` `ANTHROPIC_MODEL=` `TRADER_MODE=` `TRADER_ENV=` `TRADER_LIVE_CONFIRM=`（全て値なし）
- 対応 AC: AC-1, AC-23（対象ディレクトリが空でも exit 0）, AC-27, AC-29, AC-30（AC-31 は既に満たされている。`CLAUDE.md` は触らない）
- テスト方針: `tests/unit/test_cli.py` で 7 コマンドの `--help` が exit 0。`check_no_float_money.py` 自体の検出ロジックは `tests/unit/test_check_no_float.py` で一時ファイルに対して検証
- 依存: なし
- 並列可: なし

### T-2: domain

- 目的: 以降の全モジュールが使う型を frozen で定義する
- ファイル: `src/trader/domain/{__init__,result,money,clock,models,retry}.py`, `tests/unit/domain/{test_immutability,test_money,test_retry}.py`
- 内容:
  - `money.py`: `Money`（`Decimal`、`quantize(Decimal("0.01"), ROUND_HALF_EVEN)`）、`Price`（小数 4 桁）、`Quantity`（整数株。非整数は `Err`）、`pct_of(capital: Money, pct: Decimal) -> Money`、`to_canonical(d) -> str` / `from_canonical(s) -> Result`。`float` を一切使わない
  - `models.py`: spec データモデルの各テーブルに対応する frozen dataclass + `Action`, `Origin`, `RuleCheck`, `OrderStatus`, `Approver`, `ExitReason` の `StrEnum`。更新は `dataclasses.replace` で新オブジェクトを返すヘルパ
  - `retry.py`: `RetryPolicy(max_attempts, base_delay_s, max_delay_s)` と `backoff_delays(policy) -> tuple[Decimal, ...]`
- 対応 AC: AC-26, AC-23
- テスト方針: 全モデル・`Money` 系に対して属性代入が `FrozenInstanceError` / `ValidationError`。`pct_of(500, 15) == 75.00`、`pct_of(500, 3) == 15.00`、`pct_of(500, 6) == 30.00`。`Quantity("1.5")` が Err
- 依存: T-1
- 並列可: なし

### T-3: config

- 目的: モード判定と秘密情報の読み込みを、live を絶対に誤って起動しない形で作る
- ファイル: `src/trader/config/{__init__,mode,settings}.py`, `tests/unit/config/{test_mode_guard,test_key_separation,test_settings_validation}.py`
- 内容:
  - `mode.py`: `decide_mode(mode_env: str | None, trader_env: str | None, live_keys_present: bool, live_confirm: str | None) -> Result[TradingMode, ConfigError]`。省略 → paper。`TRADER_ENV=test` は live を無条件拒否
  - `settings.py`: `CommonSettings`（`ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `TRADER_DB_PATH`=`var/trader.sqlite3`, `TRADER_RULES_PATH`=`rules/trading-rules.yaml`）、`PaperSettings`（`ALPACA_PAPER_*` 4 つ）、`LiveSettings`（`ALPACA_LIVE_*` 4 つ + `TRADER_LIVE_CONFIRM`）。`load_settings(environ) -> Result[PaperSettings | LiveSettings, ConfigError]`。エラーメッセージは「不足している変数名」だけを含み、値を含まない
- 対応 AC: AC-13, AC-14（settings 側）, AC-25（settings 側）
- テスト方針: `monkeypatch.setenv` で組み合わせ表（mode 省略 / live + キー無し / live + キー有り + confirm 無し / 全部揃い + `TRADER_ENV=test`）。`PaperSettings` のフィールド名に `LIVE` を含むものが無いこと。エラー文字列にキー値が含まれないこと
- 依存: T-2
- 並列可: T-4, T-5

### T-4: ledger 基盤

- 目的: 「記録できない売買は実行しない」の土台。全テーブルと書き込み経路
- ファイル: `src/trader/ledger/{__init__,db,repository,portfolio_repository,source_repository}.py`, `src/trader/ledger/migrations/0001_init.sql`, `tests/unit/ledger/{test_db,test_decision_record,test_fills}.py`
- 内容:
  - `db.py`: `open_db(path) -> Result[Connection, LedgerError]`、`apply_migrations`（`schema_migrations` テーブルで冪等）、`transaction(conn)` コンテキストマネージャ（例外で rollback して `Err`）
  - `repository.py`: `insert_decision / insert_approval / insert_order / update_order_status / insert_fill / find_valid_approval(decision_id, now)`。金額は `to_canonical` で TEXT
  - `portfolio_repository.py`: positions upsert / trades insert / equity_snapshots upsert（当日 1 行、`peak_equity` の max 更新）/ engine_state get・set / cycles insert・finish
  - `source_repository.py`: sources insert、mentions insert（`(source_id, external_id, ticker)` UNIQUE で重複排除）
  - 全クエリは `?` プレースホルダ。文字列連結禁止
- 対応 AC: AC-17, AC-19
- テスト方針: 一時ファイル DB。decision に evidence 参照・`llm_model`・両ハッシュ・`rule_set_sha256`・`rule_check` が保存され `skip` / `rejected` も残る。fills: 部分約定 2 行 → `partially_filled` → `filled`、`canceled` / `rejected` の遷移。トランザクション途中の例外で何も残らない
- 依存: T-2
- 並列可: T-3, T-5

### T-5: rules 定義と lock

- 目的: 人間が書いたルールを検証済み frozen オブジェクトとして読み、承認なしの変更で起動しないようにする
- ファイル: `src/trader/rules/{__init__,schema,lock}.py`, `rules/trading-rules.yaml`, `src/trader/cli.py`（`rules approve` 本体）, `tests/fixtures/rules/*.yaml`, `tests/unit/rules/{test_lock,test_immutable_rules,test_derived_limits,test_schema_validation}.py`
- 内容:
  - `schema.py`: `RuleSet` と入れ子（`PositionRules`, `ExitRules`, `LossLimitRules`, `EntryRules`, `CostAssumptions`）を `frozen=True, extra="forbid"`。pct は `Decimal` で `0 < x <= 100`（`stop_loss_pct` は `-100 < x < 0`）、`capital_usd > 0`、`approved_by` 非空は `rules approve` 時のみ必須（ロード時は空を許す）。`DerivedLimits`（`max_notional_per_ticker`, `max_daily_loss`, `max_weekly_loss`, `max_drawdown_pct`）を `pct_of` で導出
  - `lock.py`: lock は `sha256 / approved_by / approved_at` の 3 行テキスト。`verify_lock` は不一致・欠落を `Err` にする
  - `rules approve`: YAML を検証 → `approved_by` 空なら Err → lock 書き込み → `rule_sets` テーブルに追記（T-4 の repository を使う。DB 未初期化なら初期化）
  - `rules/trading-rules.yaml`: spec の初期値をコメント込みで転記
- 対応 AC: AC-9, AC-10, AC-11（DerivedLimits 側）, AC-25（rules 側）
- テスト方針: lock 不一致 → Err、`rules approve` 後のみ一致。`RuleSet` の属性代入が例外。`position.max_notional_per_ticker_usd` / `loss_limits.max_daily_loss_usd` の混入・pct 101・capital 0 と `-500` が人間向けメッセージ（キー名を含み、スタックトレースを含まない）で拒否。capital 500 → 75.00 / 15.00 / 30.00
- 依存: T-2（`rules approve` の DB 追記部分は T-4 を使う。T-4 未完了で並列する場合は DB 追記を T-12 の `status` と同じタイミングに回し、lock 書き込みだけ先に実装する）
- 並列可: T-3, T-4

### T-6: rules 判定

- 目的: 買い前・保有中・損失上限の 3 種の判定を純粋関数で作る（LLM に依存しない）
- ファイル: `src/trader/rules/{entry_checks,exit_checks,loss_limits}.py`, `tests/unit/rules/{test_entry_checks,test_exit_checks,test_loss_limits}.py`
- 内容:
  - `entry_checks.check_entry(proposal, rules, limits, holdings, market_open, latest_price, avg_volume, loss_state) -> Result[EntryPlan, RuleRejection]`。順序: 対象外 → 既保有 → 同時保有数 → 市場時間 → 損失上限到達 → 流動性（`min_price_usd`, `min_avg_daily_volume`）→ 1 株が上限超 → 株数 = `floor(limit / price)`。`RuleRejection.reason` の例: `notional 80.00 > limit 75.00 (15% of 500)`
  - `exit_checks.check_exit(position, price, rules, now) -> ExitSignal | None`。優先順: 損切り → 保有期間 → 部分利確（未実施のみ）→ トレーリング（`partial_tp_done` のみ、`high_watermark × (1 − pct/100)`）
  - `loss_limits.evaluate(limits, realized_today, unrealized_now, realized_week, equity, peak_equity) -> LossLimitBreach | None`。日次・週次は「実現 + 評価」の合計。日次 = 米国東部時間の暦日、週次 = 同じく月曜始まり（リスク参照）
- 対応 AC: AC-11, AC-12, AC-18（判定部分）
- テスト方針: 各拒否条件に 1 テスト以上、拒否理由に導出額と比率の両方を含む。$100 買い → $150 → $120 でトレーリング発火、$100 → $105 → $84 で損切りが先に発火（spec の例そのまま）。1 株 $80 で上限 $75 → 見送り。loss: 日次 −15.00 到達 / 週次 −30.00 到達 / DD 15% 到達 / いずれも未達
- 依存: T-5
- 並列可: T-7, T-8, T-9, T-10, T-11

### T-7: sources と `ingest`

- 目的: 集約データを検証して取り込み、銘柄別 evidence を導出する
- ファイル: `src/trader/sources/{__init__,adapter,evidence}.py`, `src/trader/sources/serenity/{__init__,schema,adapter}.py`, `src/trader/cli.py`（`ingest` 本体）, `tests/fixtures/serenity/*`, `tests/unit/sources/{test_serenity_adapter,test_adapter_registry,test_evidence}.py`
- 内容:
  - `adapter.py`: `SourceAdapter` Protocol と `AdapterRegistry`（`register(adapter) -> AdapterRegistry` は新インスタンスを返す。`get(source_id) -> Result`）。既定レジストリは `serenity` のみ
  - `serenity/schema.py`: `Tweet` モデル（`model_config = ConfigDict(extra="ignore")`）。`created_at` が無ければ `timestamp` を使う。ticker ホワイトリスト: 各行の先頭トークンが `^[A-Z]{1,5}$` のものを採用（形式が想定と違えばリスク参照）
  - `serenity/adapter.py`: 全件検証してから返す（1 件でも不正なら `IngestError(invalid_count=n, first_errors=[...])` で何も返さない）。`$TICKER` 正規表現 + ホワイトリスト一致のみ `Mention` 化。1 ツイート複数銘柄 → 複数 `Mention`。`text_excerpt` は 280 文字まで
  - `evidence.py`: `build_evidence(mentions, now, window_days=14) -> tuple[Evidence, ...]`。銘柄別に初出・直近・総回数・直近 14 日回数・その前 14 日回数・直近抜粋 3 件
  - `ingest`: ファイル SHA-256 を `sources` に記録。既に同じ SHA なら「変更なし」で exit 0（冪等）
- 対応 AC: AC-4, AC-5, AC-6
- テスト方針: 不正混在ファイルで Err + 件数、DB に 0 行。ダミー `SourceAdapter` を登録して取り込める。evidence の日付・回数が fixture の期待値と一致。`ingest` の CliRunner テスト（正常 / 不正）
- 依存: T-4
- 並列可: T-6, T-8, T-9, T-10, T-11

### T-8: market data

- 目的: 株価・営業時間を `MarketDataProvider` の背後に置く
- ファイル: `src/trader/market/{__init__,data_provider,alpaca_data,fake_data}.py`, `tests/unit/market/test_alpaca_data.py`
- 内容:
  - `data_provider.py`: Protocol。`daily_bars(ticker, days=20) -> Result[tuple[Bar, ...], MarketError]`, `latest_price(ticker) -> Result[Price, MarketError]`, `market_clock() -> Result[MarketClock, MarketError]`（`is_open`, `next_open`, `next_close`。UTC aware datetime）
  - `alpaca_data.py`: read キーで `StockHistoricalDataClient`（IEX フィード）と `TradingClient`（`get_clock` のみ）を構築。SDK クライアントは注入可能。レスポンスは `Any` で受けて pydantic モデルへ変換（`float` を `Decimal(str(x))` で受ける。`market/` は AC-23 の対象外だが `float` 型注釈は境界の受け口だけに限定）。GET は `RetryPolicy(3, 0.5, 4)` + タイムアウト 10 秒
  - `fake_data.py`: `FakeMarketData(prices: Mapping[str, Price], bars, clock: MarketClock)`。`with_price(ticker, price)` で新インスタンス
- 対応 AC: AC-24（market の GET リトライ）, AC-25（レスポンス検証。`test_response_validation.py` は broker 側 T-9 だが、market の検証は本タスクの `test_alpaca_data.py` で扱う）
- テスト方針: 偽クライアントが 2 回失敗 → 3 回目成功でリトライされる。4 回失敗で Err。不正レスポンス（負の価格・欠落フィールド）→ Err。タイムアウトが SDK 呼び出しに渡ること
- 依存: T-3
- 並列可: T-6, T-7, T-9, T-10, T-11

### T-9: broker

- 目的: 発注を `Broker` の背後に隠し、URL・キー・リトライの制約を型と構築時検証で強制する
- ファイル: `src/trader/broker/{__init__,broker,alpaca_broker,fake_broker}.py`, `tests/unit/broker/{test_alpaca_urls,test_retry_policy,test_response_validation,test_fake_broker}.py`
- 内容:
  - `broker.py`: Protocol。`submit_market_order(req: OrderRequest) -> Result[BrokerOrder, BrokerError]`, `cancel_all_open() -> Result[int, BrokerError]`, `get_order(client_order_id) -> Result[BrokerOrder, BrokerError]`, `positions()`, `account()`
  - `alpaca_broker.py`: `AlpacaBroker.create(mode, trade_key, trade_secret, client_factory=...) -> Result[AlpacaBroker, BrokerError]`。URL は `mode` から固定（paper: `https://paper-api.alpaca.markets`, live: `https://api.alpaca.markets`）。`url_override` を受け取る引数を**持たない**。alpaca-py 内部リトライは無効化（リスク参照）し、GET のみ `RetryPolicy` で自前リトライ。`submit` は 1 回だけ、失敗は `Err` で返し再送しない。`client_order_id` は `order_{decision_id}`。レスポンス（`id`, `status`, `filled_qty`, `filled_avg_price`）を pydantic 検証
  - `fake_broker.py`: `FakeBroker(scenario)`。`submit` の記録、`get_order` の呼び出し回数で `partially_filled → filled` を進める。`fail_next_submit()` 等は新インスタンスを返す
- 対応 AC: AC-14（URL 側）, AC-24（broker 側）, AC-25（broker 側）
- テスト方針: paper/live で URL が決まり、他の URL を渡す手段が無い（コンストラクタ引数に `url` が無いことを `inspect.signature` で確認）。read キーだけでは構築できない。偽クライアントで `submit` が例外 → 呼び出し 1 回のみ・`Err`。`get_order` は 2 回失敗後成功。不正レスポンス → Err で値を含まないメッセージ
- 依存: T-3
- 並列可: T-6, T-7, T-8, T-10, T-11

### T-10: analysis（LLM）

- 目的: LLM を「構造化出力を返す読み取り専用の助言者」に閉じ込める
- ファイル: `src/trader/analysis/{__init__,output_schema,prompt,llm_client,analyst}.py`, `tests/unit/analysis/{test_output_schema,test_prompt,test_llm_client}.py`
- 内容:
  - `output_schema.py`: `LlmProposal(ticker, action: buy|hold|skip, confidence: 0..1 Decimal, rationale, evidence_mention_ids)` と `LlmResponse(proposals)`。`extra="forbid"`。ticker は候補リスト内のみ許可（不明銘柄は拒否）。`sell` は受け付けない（売りはルール優先。R-13）
  - `prompt.py`: `build_prompt(rules_text, evidences, bars_by_ticker, candidates) -> Prompt`。ルールは `RuleSet` から生成した読み取り専用の要約テキスト（YAML ファイルのパス・名前は書かない）。Q4 の内容: 直近 14 日の新規言及銘柄、言及回数の増減、抜粋、直近 20 日の日足、出来高平均。出力は JSON のみを要求
  - `llm_client.py`: `AnthropicClient(api_key, model, client_factory=...)`。`complete(prompt) -> Result[LlmRaw, LlmError]`。タイムアウト 60 秒、`RetryPolicy(2, 1, 8)`（429 / 5xx / タイムアウトのみ）。prompt / response の SHA-256 を `LlmRaw` に持つ。ファイル I/O を持たない
  - `analyst.py`: `propose(client, prompt) -> AnalysisResult`（検証済み proposals + `llm_model` + 両ハッシュ。検証失敗は proposals 空で `skipped_reason` 付き）。1 サイクル 1 回の呼び出し（候補をまとめて渡す）
- 対応 AC: AC-7, AC-10（`analysis/` に書き込みコード無し）, AC-24（LLM 側）
- テスト方針: スキーマ違反 / `max_notional_per_ticker_pct` などルール名を含む未知フィールド / 候補外 ticker / `sell` が採用されない。プロンプト文字列に `trading-rules` が含まれない。偽クライアントで 429 → リトライ、400 → リトライ無し、タイムアウト引数が渡る
- 依存: T-5
- 並列可: T-6, T-7, T-8, T-9, T-11

### T-11: ledger trade_closer

- 目的: 約定からポジションと閉じた trade を導出する
- ファイル: `src/trader/ledger/trade_closer.py`, `tests/unit/ledger/test_trade_closer.py`
- 内容: `apply_fill(position | None, fill, decision, now) -> PositionUpdate`（新 `Position` または `ClosedTrade`）。買い: 平均取得単価を Decimal で再計算、`opened_at` は初回 fill。売り: 実現損益 = `(price − avg_cost) × qty − fee`、qty 0 で `Trade`（`exit_reason` は decision の `origin` / `ExitSignal` 名、`holding_days` は暦日差）。部分利確後は `partial_tp_done=True`
- 対応 AC: AC-20
- テスト方針: 買い 7 株 @10 → 売り 7 株 @8 で `realized_pnl = −14.00`、`exit_reason = stop_loss`、`holding_days` 一致。部分利確 → 残り → トレーリング売りで `exit_decision_ids` が 2 件
- 依存: T-4
- 並列可: T-6〜T-10

### T-12: engine 基盤（approval / order_executor / kill_switch）

- 目的: 「記録してから送信」「承認なしで送信しない」「損失上限で止まる」を唯一の経路に固定する
- ファイル: `src/trader/engine/{__init__,approval,order_executor,kill_switch}.py`, `src/trader/cli.py`（`approve`, `resume`, `status` 本体）, `tests/unit/engine/{test_approval,test_record_before_submit,test_kill_switch,test_no_bypass}.py`
- 内容:
  - `approval.py`: `resolve_approval(decision, mode, repo, clock, next_session_close) -> Result[ApprovedOrderRequest, ApprovalError]`。paper: `approver=system` を作成。live: `find_valid_approval` で `approver=kyo` かつ `expires_at > now` を要求。`expires_at` = 承認時刻が市場時間内ならその日の `next_close`、時間外なら翌営業日の `next_close`。`ApprovedOrderRequest` はこのモジュールのファクトリ以外から生成しない（`__init__` を private 化した frozen dataclass + `_make` 関数）
  - `order_executor.py`: `execute(req: ApprovedOrderRequest, repo, broker, clock) -> Result[Order, ExecError]`。トランザクション内で decision / approval / order(`recorded`) を書き、コミット成功後に限り `broker.submit_market_order`。送信失敗 → `failed` + `last_error`。再送しない
  - `kill_switch.py`: `trigger(breach, broker, repo, clock)`: `cancel_all_open` → `engine_state` に `halted=true / halted_reason / halted_at` → `Ok`。`resume(repo)`: `halted` を消す（`peak_equity` は触らない）
  - CLI: `approve <decision_id>`（live 用。paper でも実行可だが不要）、`resume`、`status`（halted / 保有 / 直近 cycle）
- 対応 AC: AC-8（型ゲート + 静的走査）, AC-15, AC-16, AC-18（停止と永続化）
- テスト方針: repository を差し替えて 3 種の insert のそれぞれで例外 → `FakeBroker.submit` の呼び出し 0。paper で system 承認自動、live で承認なし / 期限切れ → 送信なし、有効承認 → 送信。kill switch: 未約定 2 件がキャンセルされ、`engine_state.halted` が再オープンした DB でも `true`。`test_no_bypass`: `src/trader` を走査して `.submit_market_order(` の呼び出しが `engine/order_executor.py` 以外に無い、`ApprovedOrderRequest` が `rule_check=rejected` の decision から作れない
- 依存: T-6, T-9, T-11
- 並列可: T-14

### T-13: engine cycle と `run-cycle`

- 目的: データフローの 1〜7 を 1 コマンドに繋ぐ
- ファイル: `src/trader/engine/{holdings,candidates,cycle}.py`, `src/trader/cli.py`（`run-cycle` 本体）, `tests/unit/engine/test_cycle.py`
- 内容:
  - `holdings.py`: 保有ごとに `latest_price` → 評価額集計 → `high_watermark` 更新（下げない）→ `check_exit` → `rule_exit` decision（`rule_check=passed`, LLM 呼ばず）
  - `candidates.py`: `build_evidence` → 候補選定（直近 14 日に言及あり、既保有・対象外を除く、最大 20 銘柄）→ `daily_bars` → `analyst.propose` → 各 proposal に `check_entry` → decision（passed / rejected / skip）
  - `cycle.py`: `CycleDeps`（settings, rules, limits, repo 群, broker, market, llm, clock）frozen dataclass。`run_cycle(deps) -> Result[CycleOutcome, CycleError]`。順序: lock 検証 → `cycles` 開始行 → halted なら判断のみ → equity snapshot upsert → `loss_limits.evaluate` → 到達で `kill_switch.trigger` して終了 → holdings → candidates → 市場時間外なら発注せず終了 → approval → executor → 約定ポーリング（最大 60 秒、2 秒間隔、`clock` 注入）→ `trade_closer.apply_fill` → `cycles` 終了行。`var/run-cycle.lock` を `fcntl.flock(LOCK_EX | LOCK_NB)` で取り、取れなければ Err
  - 部分失敗: LLM Err → 全候補 `skip` で続行。`market_clock` / `latest_price` Err → 新規買いなし、snapshot 更新なし、保有は次サイクル。DB Err → `outcome=error`
  - CLI `run-cycle`: `TRADER_ENV=test` では実行を拒否する（テストは `run_cycle` を直接呼ぶ。N-9）
- 対応 AC: AC-8（フロー側）, AC-18（次サイクルで発注しない）, AC-28 の前提（R-8）
- テスト方針: FakeBroker + FakeMarketData + 偽 LLM + FixedClock。halted 状態で LLM の buy があっても executor 未呼び出し。市場時間外は decision が残り order が無い。LLM Err で `skip` decision が候補数だけ残る。マーケットデータ Err で `equity_snapshots` が増えない。ロック取得済みで 2 回目が Err
- 依存: T-7, T-8, T-10, T-12
- 並列可: なし

### T-14: report

- 目的: kyo が「何を根拠に買い、なぜ売り、いくら儲かったか」を読める出力
- ファイル: `src/trader/report/{__init__,metrics,live_estimate,render}.py`, `src/trader/cli.py`（`report` 本体）, `tests/unit/report/{test_metrics,test_live_estimate,test_render}.py`
- 内容:
  - `metrics.py`: `compute(trades, decisions, snapshots, positions, period) -> Metrics`（期間損益、勝率、平均損益、最大 DD（snapshots から）、ルール発火回数（`exit_reason` 別）、判断内訳 passed / rejected / skip / hold）
  - `live_estimate.py`: Q10 の式。`fills` の約定金額合計 × slippage、`min(約定金額 × commission_pct, cap)`（cap 空なら上限なし）、注文数 × 固定、`capital × fx × 2`
  - `render.py`: 見出し付きテキスト。trade ごとに「根拠（entry decision の rationale 先頭 200 字 + evidence 件数）/ 売却理由 / 損益 / 保有日数」
  - CLI `report [--from --to]`
- 対応 AC: AC-21, AC-22, AC-33（人間確認。render の形式）
- テスト方針: 既知の trade 3 件（+20, −14, +6）で期間損益 12.00、勝率 66.67%、平均 4.00。snapshots 500 → 520 → 470 で最大 DD 9.62%。live_estimate: spec の係数と fills 合計 $150 で手計算値と一致。render 出力に `rationale` / `exit_reason` / `realized_pnl` が含まれる
- 依存: T-11
- 並列可: T-12

### T-15: 統合テストとカバレッジ

- 目的: 一気通貫で trade が 1 件閉じることを実 SQLite で証明し、全 AC の verify を通す
- ファイル: `tests/integration/test_paper_cycle.py`, `tests/integration/conftest.py`（必要なら）, カバレッジ不足箇所のテスト追加（`tests/unit/**`）
- 内容: 一時ディレクトリに DB / rules YAML（capital 500, `approved_by: kyo`）/ lock / tweets fixture を置く。`ingest` → `run_cycle`（FakeMarketData: 候補銘柄 $10、市場オープン。偽 LLM: buy。期待: `floor(75/10) = 7` 株の order が `filled`）→ `FakeMarketData.with_price($8)`（−20% < 損切り −15%）→ `run_cycle`（`rule_exit` decision、LLM 未呼び出し、sell 7 株 filled）→ `report` で trade 1 件、`realized_pnl = −14.00`、`exit_reason = stop_loss`。最後に spec の全 verify を実行し、失敗があれば該当タスクに戻す
- 対応 AC: AC-3, AC-28（+ 全 AC の最終確認）
- テスト方針: 上記シナリオ。カバレッジは `--cov=src --cov-fail-under=80`。`cli.py` の各コマンドは CliRunner で最低 1 パスずつ通す
- 依存: T-13, T-14
- 並列可: なし

## リスク

spec に根拠があるものだけを挙げる。「未確認」は隠さない。

1. **alpaca-py の内部リトライが POST にも効く可能性（N-4 違反）**: alpaca-py の REST クライアントは 429 / 504 を内部で再試行する実装があり、既定ではこれが発注 POST にも適用されうる。T-9 で `retry_attempts=0`（実際の引数名は builder が SDK ソースで確認）にして無効化し、GET だけ `domain/retry.py` で自前リトライする。無効化できない場合は alpaca-py の `TradingClient` を使わず `requests` で直接叩く方針に切り替え、reviewer に報告する
2. **alpaca-py / anthropic の型情報と mypy strict**: 両 SDK は型ヒント付きだが、`Any` が返る箇所がある。境界（`alpaca_data.py`, `alpaca_broker.py`, `llm_client.py`）で `Any` を受けて pydantic 検証し、内部に `Any` を出さない（N-5）。それでも通らなければ `[[tool.mypy.overrides]]` を**そのモジュールのみ**に限定して緩める
3. **Alpaca の約定情報に手数料が無い**: `get_order` は `filled_qty` / `filled_avg_price` を返すが手数料は返さない（Alpaca は $0 手数料。SEC / FINRA fee は別の activities API）。v1 は `fills.fee = 0` で記録し、FakeBroker のテストで fee 付きの経路を検証する。live 移行の spec で activities 連携を扱う
4. **市場時間・タイムゾーン**: 「市場時間内」「次のセッション終了」は Alpaca の clock / calendar（America/New_York）に依存。日次・週次損失の区切りは spec が明示していないため、本 plan では **日次 = 米国東部時間の暦日、週次 = 同じく月曜始まり** と決めた。kyo が別の区切りを望むなら承認前に指摘してほしい
5. **ticker_stats.txt の形式が未確認**: yan-labs のファイルは git 管理外で本 plan 作成時に参照できない。T-7 は「各行の先頭トークンが大文字 1〜5 文字」を採用する寛容なパーサにする。実ファイルで銘柄が 0 件になる場合は AC-32 の手動確認で判明するので、kyo が形式を報告して T-7 だけ直す
6. **Anthropic API のコストと時間（N-10）**: 15 分間隔 × 市場時間 6.5 時間 = 1 日 26 回。1 サイクル 1 回の呼び出し（候補をまとめて渡す）に固定し、候補が無いサイクルは呼ばない。タイムアウト 60 秒 + リトライ 2 回で最悪 3 分。ポーリング 60 秒と合わせて 5 分以内に収まる想定。超える場合は候補上限 20 を下げる
7. **カバレッジ 80%（AC-3）**: `alpaca_broker.py` / `alpaca_data.py` / `llm_client.py` / `cli.py` は偽クライアント・CliRunner で覆う。それでも足りない場合、T-15 でテストを追加する（`# pragma: no cover` で逃げない）
8. **SQLite の同時実行**: `run-cycle` と `approve` / `report` が同時に走りうる。WAL モード + `busy_timeout=5000` + `run-cycle` のロックファイルで対処。`approve` は短いトランザクション 1 つなので衝突は限定的
9. **AC-10 の grep が `analysis/` のコメントにも効く**: verify は `trading-rules` という文字列を `src/trader/analysis/` から探す。builder はコメント・docstring にもこの文字列を書かないこと（T-10 のテストでも検査する）
10. **live 経路は paper では実行されない**: `LiveSettings` / kyo 承認 / 期限切れ は unit テストでしか通らない。実口座接続は spec のスコープ外（段階 (2) の spec で実施）
11. **`CLAUDE.md` の出力例**: 「初回の実装後に実際の出力へ差し替える」とあるが builder は `CLAUDE.md` を編集できない。T-1 完了後に kyo（または /plan 側）が差し替える。AC-31 には影響しない（`TODO` は無い）
12. **依存バージョン**: ネットワーク未接続で最新版を確認できていないため上記は範囲指定。`uv sync` で解決できなければ T-1 で範囲を広げ、`uv.lock` を正とする

## Proof

「この計画が完了した」= spec の全 verify が exit 0（Stop hook が判定）+ AC-32 / AC-33 を kyo が Deploy 後に確認。

| 受入基準 | 証明方法 | 対応タスク |
|---|---|---|
| AC-1 | `uv run ruff check . && uv run ruff format --check .` | T-1（以降全タスクで維持） |
| AC-2 | `uv run mypy src` | T-1（以降全タスクで維持） |
| AC-3 | `uv run pytest tests/unit -q --cov=src --cov-fail-under=80` | T-15（各タスクが積み上げ） |
| AC-4 | `tests/unit/sources/test_serenity_adapter.py`: 不正混在で Err + 件数、DB 0 行 | T-7 |
| AC-5 | `tests/unit/sources/test_adapter_registry.py`: ダミーアダプタ登録・取り込み | T-7 |
| AC-6 | `tests/unit/sources/test_evidence.py` | T-7 |
| AC-7 | `tests/unit/analysis/test_output_schema.py` | T-10 |
| AC-8 | `tests/unit/engine/test_no_bypass.py`: 型ゲート + `submit_market_order` 呼び出し箇所の静的走査 + サイクル経路 | T-12, T-13 |
| AC-9 | `tests/unit/rules/test_lock.py` | T-5 |
| AC-10 | `tests/unit/rules/test_immutable_rules.py` + `! grep ... src/trader/analysis/` | T-5, T-10 |
| AC-11 | `tests/unit/rules/test_entry_checks.py` + `test_derived_limits.py`（500 → 75.00 / 15.00 / 30.00） | T-5, T-6 |
| AC-12 | `tests/unit/rules/test_exit_checks.py` | T-6 |
| AC-13 | `tests/unit/config/test_mode_guard.py` | T-3 |
| AC-14 | `tests/unit/config/test_key_separation.py` + `tests/unit/broker/test_alpaca_urls.py` | T-3, T-9 |
| AC-15 | `tests/unit/engine/test_record_before_submit.py` | T-12 |
| AC-16 | `tests/unit/engine/test_approval.py` | T-12 |
| AC-17 | `tests/unit/ledger/test_fills.py` | T-4 |
| AC-18 | `tests/unit/engine/test_kill_switch.py`（停止・永続化・次サイクル抑止） | T-6, T-12, T-13 |
| AC-19 | `tests/unit/ledger/test_decision_record.py` | T-4 |
| AC-20 | `tests/unit/ledger/test_trade_closer.py` | T-11 |
| AC-21 | `tests/unit/report/test_metrics.py` | T-14 |
| AC-22 | `tests/unit/report/test_live_estimate.py` | T-14 |
| AC-23 | `uv run python scripts/check_no_float_money.py` | T-1（以降全タスクで維持） |
| AC-24 | `tests/unit/broker/test_retry_policy.py` + `tests/unit/analysis/test_llm_client.py` | T-9, T-10（market 側は T-8 の `test_alpaca_data.py` で補強） |
| AC-25 | `tests/unit/config/test_settings_validation.py` + `tests/unit/broker/test_response_validation.py` + `tests/unit/rules/test_schema_validation.py` | T-3, T-5, T-9 |
| AC-26 | `tests/unit/domain/test_immutability.py` | T-2 |
| AC-27 | `find src ... NR>800` | T-1（以降全タスクで維持。目安 400 行） |
| AC-28 | `tests/integration/test_paper_cycle.py`: ingest → 買い 7 株 @10 → $8 → 損切り売り → report に trade 1 件 | T-15 |
| AC-29 | `uv run trader <cmd> --help` × 7 | T-1（スタブ）→ T-5, T-7, T-12, T-13, T-14 で本体差し替え |
| AC-30 | `.env.example` の 6 変数 + `.gitignore` の `data/` `var/` | T-1 |
| AC-31 | `CLAUDE.md` Commands に `TODO` 無し（**現時点で既に合格**。変更なし） | – |
| AC-32 | **人間確認（Deploy 後）**: kyo が `.env` に paper キーを置き、`rules/trading-rules.yaml` の `approved_by/at` を記入 → `trader rules approve` → `data/sources/serenity/` に tweets.json と ticker_stats.txt をコピー → `trader ingest` → 市場時間内に `trader run-cycle` → `trader status` / `trader report` で判断と注文が記録されている | – |
| AC-33 | **人間確認（Deploy 後）**: `trader report` の出力を読み、trade ごとに根拠・売却理由・損益が追えるか | T-14（形式）、確認は kyo |

対応の無い AC: なし。

## 承認

approved_by:
approved_at:
