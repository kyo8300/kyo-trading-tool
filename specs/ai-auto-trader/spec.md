# Spec: ai-auto-trader

- 元 intent: `specs/ai-auto-trader/intent.md`（approved_by: kyo, approved_at: 2026-09-14）
- 作成日: 2026-09-13（改訂: 2026-09-14、kyo のレビュー 2 回分を反映）
- 状態: draft

> **kyo へ**: 2026-09-14 の回答（証券会社 (a) 確定 / 実弾 $2,000 は怖い）を反映した。売買ルールは**全て資金に対する割合**で定義し直し、
> 資金額 `capital_usd` の既定を **$500** に下げた。作戦（比率）は資金額に依存しないので、kyo が決めるのは資金額だけでよい。
> 承認時に確定するものは末尾「kyo が承認時に確定するもの」の 3 つ（技術スタック / スコープ / capital_usd の額とルール比率の変更有無）。
> 技術スタックの確定後は `CLAUDE.md` の Commands を更新する。

## 概要

intent の核: 集約データ（初期は yan-labs/serenity-aleabitoreddit アーカイブ）を読む AI が、kyo が決めた売買ルールの中で米株の
買い候補・買うタイミング・売るタイミングを決め、証券会社 API で実行し、全判断・約定・損益を自動記録して成績を数字で見せる。

### この spec が満たす範囲

intent の段階 (1)〜(4) のうち、**段階 (1) paper 運用の完成 + 段階 (2) 全件承認フローの土台**まで。

| 段階 | この spec | 理由 |
|---|---|---|
| (1) paper で数週間運用、成績と挙動を確認 | **含む（完成させる）** | 実弾の前提。ここが動かないと後段は評価できない |
| (2) 少額実弾、売買ごとに kyo が承認 | **土台を含む**: live モードの起動ガード、承認レコード、`approve` コマンド、承認なしでは発注しないパス | 段階 (1) と同じコードパスを使うので、ガードを後付けすると paper と live で挙動が分岐する。ただし**実際の live 口座接続・入金・初回 live 発注は本 spec のスコープ外**（paper 成績を見て kyo が判断） |
| (3) 条件付き自動 + 人間確認のハイブリッド | 含まない | 「どの条件が確実か」は記録データ（paper 数週間分）が無いと決められない。Q5/Q6 の条件だけ置き、別 spec にする |
| (4) 自動範囲の拡大 | 含まない | 同上 |

### 満たさない範囲（詳細は「スコープ外」）

日本株、楽天証券、ニュース・決算の自動解析、発信者の知識ベース自作、kyo の裁量売買の記録、段階 (3)(4) の自動化判定ロジック。

## 技術スタック

CLAUDE.md の Commands は未定。intent の制約（金額は Decimal/整数、外部データはスキーマ検証、API キー分離、paper/本番分離、記録必須）に合う候補を比較した。

| 軸 | **A: Python 3.12 + uv + pytest（推奨）** | B: TypeScript + Node 22 + vitest | C: Go 1.23 |
|---|---|---|---|
| 学習コスト | 低。kyo が読んで判断根拠を追いやすい | 低〜中。型定義は堅いが Decimal・非同期の作法が増える | 中。型は堅いが記述量が多い |
| 証券会社 SDK | `alpaca-py`（Alpaca 公式、取引 + マーケットデータ両対応）。moomoo / ウィブルの公式 SDK も Python あり。IBKR も `ib_async` あり | `@alpacahq/alpaca-trade-api`（公式だが更新が遅め）。moomoo は JS SDK あり、ウィブルは無し | コミュニティ SDK のみ |
| Decimal | 標準ライブラリ `decimal.Decimal`。追加依存なし | `decimal.js` 等の外部依存。JSON/ORM 境界で number に化けやすい | `shopspring/decimal`（外部） |
| データ処理 | `pandas` / `polars`、JSON・CSV の取り込みが容易 | 可能だがライブラリが薄い | 弱い |
| LLM SDK | `anthropic` 公式。構造化出力 + `pydantic` 検証 | 公式 SDK あり + `zod` | 公式 SDK あり |
| スキーマ検証 | `pydantic` v2（境界で `unknown`→型付け。Decimal ネイティブ対応） | `zod`（Decimal は文字列で受けて変換が必要） | 手書きが多い |
| デプロイ容易性 | 単一マシンで `uv run` + cron/systemd timer。VPS でも同じ | 同等 | バイナリ1つで最も簡単 |
| この用途への適合 | **高**。数値・データ・LLM が全部標準的な組み合わせ。個人ツールの規模に合う。候補 3 社すべてに公式 Python SDK がある | 中。UI を先に作るなら有利だが本 spec に UI は無い | 中。安全な並行処理は魅力だが本ツールは単一プロセスの定期実行で不要 |

**推奨: A（Python 3.12 + uv + pytest + ruff + mypy strict + pydantic v2 + alpaca-py + anthropic）**。
理由: 金額 Decimal が標準で、外部データ検証（pydantic）と LLM 構造化出力の相性が良く、証券会社候補 3 社すべてに公式 Python SDK がある（live 移行時にブローカーを国内業者にしても言語を変えなくてよい）。
kyo が承認時に確定する。確定後、`CLAUDE.md` の `## Commands` を以下に更新する（本 spec の verify コマンドはこれを前提にしている）:

```
- Build: uv sync
- Test: uv run pytest tests/unit -q（unit）/ uv run pytest tests/integration -q（integration）
- Lint: uv run ruff check . && uv run ruff format --check .
- Typecheck: uv run mypy src
```

主要依存（バージョンは plan で固定）: `alpaca-py`, `anthropic`, `pydantic>=2`, `pydantic-settings`, `typer`（CLI）, `pyyaml`（ルール定義）。
DB は標準の `sqlite3`（単一ユーザー・単一プロセス。パラメータ化クエリのみ）。dev: `pytest`, `pytest-cov`, `ruff`, `mypy`, `respx`/`pytest-httpx` 相当のモック。

## 機能要件

### 集約データの取り込み（intent: 触れるもの / 制約「外部データの扱い」）

- R-1: yan-labs アーカイブ（`data/aleabitoreddit_tweets.json`）を**手動コピー**したファイルを取り込む。`npx skills add` や `skills update` は使わず、SKILL.md の自己更新指示は取り込まない（Q1）
- R-2: 取り込み時に pydantic スキーマで検証し、不正なレコードは件数付きで拒否する（fail fast）。検証後のデータは `source_id`（例: `serenity`）を付けて正規化テーブルに保存する
- R-3: 情報源は `SourceAdapter` インターフェース経由で追加できる。初期実装は `serenity` の1つのみ（Q9 の追加基準は運用で決める）
- R-4: 取り込んだ集約データから、銘柄ごとに「言及履歴（初出日・直近言及日・言及回数）」と「言及の抜粋」を導出し、判断の入力（evidence）として使えるようにする

### 判断（intent: 望む結果 1・4、制約「外部データの扱い」「売買ルールは人間が決める」）

- R-5: AI（LLM）は集約データの evidence と株価データを入力に、**買い候補 / 買う・売る・見送るのアクション / 根拠 / 確信度**を構造化出力（JSON）で返す。出力は pydantic で検証し、検証に失敗した出力は判断として採用しない
- R-6: LLM の出力は**発注シグナルではない**。必ずルールエンジン（R-9〜R-13）を通過したものだけが注文候補になる。ルールエンジンを迂回して発注する API を設けない
- R-7: LLM に渡すプロンプトには売買ルールを読み取り専用で含める。LLM の出力スキーマにルールを変更するフィールドは存在せず、ルールに反する出力（例: 上限超えの数量）はルールエンジンが拒否する
- R-8: 判断は 1 サイクル = 1 回の `run-cycle` 実行として行い、cron/systemd timer 等で**市場時間内 15 分間隔**で定期実行する（デーモンにしない）。市場時間外のサイクルは判断のみ行い発注しない

### 売買ルール（intent: 望む結果 3、制約「売買ルールは人間が決める」）

- R-9: 売買ルールは `rules/trading-rules.yaml` に人間が書く。項目: 運用資金 `capital_usd`、利確（部分利確 + トレーリング）、損切り、1 銘柄あたり上限（**capital に対する %**）、同時保有数上限、保有期間上限、1 日・1 週間の最大損失（**capital に対する %**）、口座全体の最大ドローダウン（%）、取引対象外リスト。`approved_by` / `approved_at` を必須にする。**金額の上限は全て `capital_usd × pct` から起動時に導出**し、YAML に絶対額は書かない（作戦 = 比率は資金額に依存せず、kyo が決めるのは資金額だけ）
- R-10: ルールファイルの SHA-256 を `rules/trading-rules.lock` に記録し、起動時に一致を検証する。不一致（承認なしの変更）なら起動しない。lock の更新は kyo が `trader rules approve` を実行したときだけ行う
- R-11: ルールはイミュータブルなオブジェクトとしてロードし、実行中に変更する API を持たない。LLM モジュールはルールファイルにも lock にも書き込めない（ファイルアクセスを持たない設計）
- R-12: 買いの前に検証: 対象外銘柄でない / 同時保有数未満 / 1 銘柄上限（capital × `max_notional_per_ticker_pct`）以下 / 既に保有していない / 日次・週次・ドローダウンの損失上限に未到達 / 市場時間内
- R-13: 保有中は毎サイクルで検証: 損切り価格到達 → 売り / 部分利確到達 → 一部売り / トレーリングストップ到達 → 残り売り / 保有期間上限到達 → 売り。売りは LLM の判断を待たない（ルールが優先）

### 発注・約定（intent: 望む結果 2、制約「paper から始め」「記録は必須」）

- R-14: 証券会社は `Broker` インターフェースの背後に隠し、実装を差し替えられるようにする。**v1（段階 (1) paper）は Alpaca**（2026-09-14 kyo 確定）。初期実装は `AlpacaBroker` + テスト用 `FakeBroker`。段階 (2) の live 移行時に実口座を Alpaca / moomoo証券 / ウィブル証券のどれにするか決め直す（国内 2 社が本命。比較は「設計 > 証券会社の比較」）
- R-15: 実行モードは `TRADER_MODE=paper|live`。省略時は `paper`。`live` は (a) `TRADER_MODE=live` (b) live 用キーが揃っている (c) `TRADER_LIVE_CONFIRM=I_ACCEPT_REAL_MONEY_RISK` の3つが揃わなければ起動を拒否する。テスト実行中（`TRADER_ENV=test`）は条件に関わらず `live` を拒否する
- R-16: 注文は「判断レコード → 承認レコード → 注文レコード」の順で DB に**書き込み成功してから**ブローカーへ送信する。いずれかの書き込みに失敗したら送信しない（記録できない売買は実行しない）
- R-17: 承認: `paper` モードでは `approver=system` の承認レコードを自動作成する。`live` モードでは kyo が `trader approve <decision_id>` を実行した承認レコードが無い注文は送信しない（段階 (2) の全件承認）。承認の有効期限は**次の市場セッション終了まで**（承認時刻が市場時間内ならその日の大引け、時間外なら翌営業日の大引け）。期限切れの承認では送信しない
- R-18: 送信後、約定をポーリングして約定レコード（数量・約定価格・手数料・時刻）を保存する。部分約定・拒否・キャンセルも状態として記録する
- R-19: 日次・週次の損失上限（capital × pct）、または最高評価額からのドローダウン上限に達したら **キルスイッチ**: 未約定注文を全キャンセル、新規発注を停止、状態 `halted` を DB に永続化、理由をログと記録に残す。再開は kyo が `trader resume` を実行したときのみ。損失は実現損益 + 評価損益の合計で判定する

### 記録と成績（intent: 望む結果 4、Q10）

- R-20: 全ての判断（採用・見送り・ルール拒否を含む）に、入力 evidence の参照、LLM モデル ID、プロンプトとレスポンスのハッシュ、ルール検証の結果、を紐づけて記録する
- R-21: 各ポジションについて「何を根拠に買ったか / いつ・なぜ売ったか（どのルールが発火したか、または LLM 判断か）/ 損益」を1つの `trade` として閉じたときに記録する
- R-22: `trader report` で成績を出力する: 期間損益、勝率、平均損益、最大ドローダウン、保有中ポジション、ルール発火回数、判断のうち採用・見送り・拒否の内訳
- R-23: paper 成績には**実弾期待値の概算**を並べる: 想定スリッページ・手数料・為替コストを差し引いた損益（係数はルールファイルの `cost_assumptions`。初期値は本 spec で決定、live ブローカー確定時に手数料・為替の項目だけ合わせる）

### 運用（intent: 望む結果 5）

- R-24: CLI サブコマンド: `ingest`（取り込み）/ `run-cycle`（1 サイクル）/ `approve`（承認）/ `report`（成績）/ `rules approve`（lock 更新）/ `resume`（キルスイッチ解除）/ `status`（現在の状態）

## 非機能要件

- N-1: **金額・株数・価格は `decimal.Decimal` のみ**。`src/` の domain / broker / ledger / rules 配下で `float` の型注釈・`float()` 呼び出し・`float` を返す関数を静的チェックで禁止する。DB には文字列（canonical decimal string）で保存する。% から導出する絶対額も Decimal 演算で行い、最小単位（USD セント）に量子化する
- N-2: **秘密情報は環境変数のみ**。名前を paper/live と read/trade で分ける: `ALPACA_PAPER_READ_KEY/SECRET`, `ALPACA_PAPER_TRADE_KEY/SECRET`, `ALPACA_LIVE_READ_KEY/SECRET`, `ALPACA_LIVE_TRADE_KEY/SECRET`, `ANTHROPIC_API_KEY`。起動時に現在のモードで必須の値だけを検証し、不足なら人間が読めるメッセージで終了する。ログ・エラー・記録に値を出さない
- N-3: paper モードのプロセスは live のキーを**読まない**（Settings がモードに応じて存在するフィールドだけ持つ）。Alpaca の paper エンドポイントと live エンドポイントは URL レベルでも分け、`AlpacaBroker` はモードから決まる URL 以外を受け付けない
- N-4: 外部 API 呼び出し（ブローカー取引・マーケットデータ・Anthropic）にはタイムアウト・リトライ（指数バックオフ、冪等な GET のみ）・レート制限を付ける。発注 POST は**リトライしない**（二重発注防止。`client_order_id` で冪等性を持たせる）
- N-5: 外部データ（ブローカーレスポンス、LLM 出力、集約データ、環境変数、ルールファイル）は境界で pydantic 検証し、内部では検証済み型だけを扱う。`Any` は境界の受け口以外で禁止
- N-6: エラーは握りつぶさない。失敗しうる処理は `Result` 型（`Ok[T] | Err[E]`）で返し、CLI 層で人間が読めるメッセージに変換する。スタックトレース・パス・キーはユーザー向けメッセージに含めない
- N-7: 全データ構造は frozen（`@dataclass(frozen=True)` または pydantic `frozen=True`）。状態変更は新しいオブジェクトを返す
- N-8: 1 ファイル 200〜400 行目安、800 行上限。関数 50 行未満
- N-9: テストカバレッジ 80% 以上。外部サービス（ブローカー / Anthropic）はモック。時刻はクロックを注入する。テストは常に `TRADER_ENV=test` で実行し、実ネットワークに出ない
- N-10: `run-cycle` は 1 回あたり 5 分以内に完了する（cron 15 分間隔の前提。R-8）。同時実行はロックファイルで防ぐ
- N-11: 集約データ・ルール・記録 DB は git 管理外（`data/`, `var/`）。`rules/trading-rules.yaml` と `.lock` は git 管理（承認履歴として残す）

## 設計

### 構成（すべて新規）

```
kyo-trading-tool/
├── pyproject.toml                 # uv / ruff / mypy / pytest 設定
├── .env.example                   # 変数名だけ（値なし）
├── rules/
│   ├── trading-rules.yaml         # 人間が書く売買ルール（初期値は下記。比率のみ、絶対額なし）
│   └── trading-rules.lock         # SHA-256 + approved_by/at（trader rules approve が生成）
├── data/sources/serenity/         # 手動コピーした yan-labs data/*.json（git 管理外）
├── var/                           # SQLite DB、ロックファイル、ログ（git 管理外）
├── src/trader/
│   ├── cli.py                     # typer エントリ。サブコマンドの薄いルーティングのみ
│   ├── config/
│   │   ├── settings.py            # pydantic-settings。モード別の必須キー検証（N-2, N-3, R-15）
│   │   └── mode.py                # TradingMode enum、live 起動条件の判定（純粋関数）
│   ├── domain/                    # 型と純粋関数だけ。I/O なし
│   │   ├── money.py               # Money/Price/Quantity（Decimal ラッパー、量子化規則）、pct → 金額の導出関数
│   │   ├── result.py              # Ok/Err
│   │   ├── models.py              # Decision, Approval, Order, Fill, Position, Trade, Evidence, EquitySnapshot
│   │   └── clock.py               # Clock プロトコル + SystemClock / FixedClock
│   ├── sources/                   # 集約データ（R-1〜R-4）
│   │   ├── adapter.py             # SourceAdapter プロトコル
│   │   ├── serenity/schema.py     # yan-labs tweets.json の pydantic スキーマ
│   │   ├── serenity/adapter.py    # 読み込み → 正規化 Mention レコード
│   │   └── evidence.py            # 銘柄別の言及履歴・抜粋を組み立てる
│   ├── market/                    # 株価データ（Q7）
│   │   ├── data_provider.py       # MarketDataProvider プロトコル（bars, latest quote, calendar）
│   │   ├── alpaca_data.py         # Alpaca Market Data API 実装
│   │   └── fake_data.py           # テスト用
│   ├── rules/                     # 売買ルール（R-9〜R-13）
│   │   ├── schema.py              # RuleSet（frozen pydantic、比率のみ）と YAML ロード。DerivedLimits（capital × pct の絶対額）を生成
│   │   ├── lock.py                # SHA-256 検証・lock 生成
│   │   ├── entry_checks.py        # 買い前検証（R-12）→ Result。上限額は DerivedLimits から受け取る
│   │   ├── exit_checks.py         # 保有中の売り判定（R-13）→ ExitSignal | None
│   │   └── loss_limits.py         # 日次/週次損失（DerivedLimits の絶対額）・最高評価額からのドローダウンの集計と判定（R-19）
│   ├── analysis/                  # LLM 判断（R-5〜R-7）
│   │   ├── llm_client.py          # Anthropic 呼び出し（タイムアウト・リトライ）。プロンプト/レスポンスのハッシュ化
│   │   ├── prompt.py              # プロンプト組み立て（ルールは読み取り専用テキストとして埋め込む）
│   │   ├── output_schema.py       # LLM 出力の pydantic スキーマ（ルール変更フィールドなし）
│   │   └── analyst.py             # evidence + 株価 → 検証済み Proposal のリスト
│   ├── broker/                    # 発注（R-14〜R-18）
│   │   ├── broker.py              # Broker プロトコル（submit, cancel, get_order, positions, account）
│   │   ├── alpaca_broker.py       # alpaca-py 実装。モードから URL 決定。発注はリトライなし
│   │   └── fake_broker.py         # テスト用（約定シミュレーション）
│   ├── ledger/                    # 記録（R-16, R-20〜R-21）
│   │   ├── db.py                  # sqlite3 接続、マイグレーション適用、トランザクション
│   │   ├── migrations/0001_init.sql
│   │   ├── repository.py          # 書き込み/読み出し（パラメータ化クエリのみ）
│   │   └── trade_closer.py        # Fill から Position/Trade を導出
│   ├── engine/                    # 1 サイクルのオーケストレーション（R-8）
│   │   ├── cycle.py               # ingest済みevidence → analyst → rules → approval → order → fill
│   │   ├── approval.py            # paper 自動承認 / live 承認レコード確認（R-17）
│   │   ├── kill_switch.py         # R-19
│   │   └── order_executor.py      # 「記録してから送信」を強制する唯一の発注経路（R-16）
│   └── report/
│       ├── metrics.py             # 損益、勝率、最大 DD（純粋関数）
│       ├── live_estimate.py       # 実弾期待値の概算（R-23）
│       └── render.py              # テキスト出力
├── scripts/
│   └── check_no_float_money.py    # AST で float 使用を検出（N-1）
└── tests/
    ├── unit/
    └── integration/               # FakeBroker + 実 SQLite で end-to-end
```

### データモデル（SQLite。金額・数量は TEXT の canonical Decimal 文字列、時刻は ISO 8601 UTC）

| テーブル | 主なカラム | 備考 |
|---|---|---|
| `sources` | `source_id` PK, `name`, `imported_at`, `file_sha256`, `record_count` | 取り込み履歴 |
| `mentions` | `id` PK, `source_id` FK, `external_id`（tweet id）, `ticker`, `posted_at`, `text_excerpt`, `url`, `raw_sha256` | 正規化した言及。1 ツイート複数銘柄なら複数行 |
| `rule_sets` | `sha256` PK, `approved_by`, `approved_at`, `capital_usd`, `content_yaml` | `rules approve` のたびに追加。判断はこの sha を参照。`capital_usd` を列に持つのは、report で「当時の資金額」に対する % 成績を出すため |
| `decisions` | `id` PK, `cycle_id`, `decided_at`, `mode`, `ticker`, `action`(`buy`/`sell`/`hold`/`skip`), `origin`(`llm`/`rule_exit`), `confidence`, `rationale`, `evidence_mention_ids` JSON, `llm_model`, `prompt_sha256`, `response_sha256`, `rule_set_sha256`, `rule_check`(`passed`/`rejected`), `rule_check_reason`, `proposed_notional`, `reference_price` | 見送り・拒否も記録。`rule_check_reason` には導出した絶対額（例: `notional 80.00 > limit 75.00 (15% of 500)`）を含める |
| `approvals` | `id` PK, `decision_id` FK, `approver`(`system`/`kyo`), `approved_at`, `expires_at` | live では `kyo` 必須。`expires_at` = 次の市場セッション終了 |
| `orders` | `id` PK, `decision_id` FK, `approval_id` FK, `client_order_id` UNIQUE, `broker_order_id`, `mode`, `side`, `qty`, `order_type`, `status`(`recorded`/`submitted`/`partially_filled`/`filled`/`canceled`/`rejected`/`failed`), `submitted_at`, `last_error` | `recorded` の後に送信 |
| `fills` | `id` PK, `order_id` FK, `filled_at`, `qty`, `price`, `fee` | 部分約定は複数行 |
| `positions` | `ticker` PK, `qty`, `avg_cost`, `opened_at`, `high_watermark`, `partial_tp_done` | fills から導出。トレーリング用の高値を持つ |
| `trades` | `id` PK, `ticker`, `opened_at`, `closed_at`, `entry_decision_id`, `exit_decision_ids` JSON, `exit_reason`, `realized_pnl`, `fees`, `holding_days` | ポジションを閉じたときに 1 行 |
| `equity_snapshots` | `snapshot_date` PK, `mode`, `cash`, `positions_value`, `equity`（= cash + positions_value）, `peak_equity`（過去最高評価額）, `drawdown_pct`, `taken_at` | **R-19 のドローダウン判定用**。毎サイクルで当日分を upsert（当日は最新値、日付が変わると新行）。`peak_equity` は前日までの最高値と当日の `equity` の max。初回は `capital_usd` |
| `engine_state` | `key` PK, `value` | `halted`, `halted_reason`, `halted_at`, `last_cycle_id` |
| `cycles` | `id` PK, `started_at`, `finished_at`, `mode`, `outcome`, `error_summary` | 1 サイクル 1 行 |

**集約データの取り込み形式（serenity）**: yan-labs `data/aleabitoreddit_tweets.json` を `data/sources/serenity/tweets.json` に手動コピー。
スキーマは最小限を必須（`id`: 数値文字列, `created_at`/`timestamp`: ISO 日時, `text`: 文字列, `url`: 任意）とし、未知フィールドは無視する。
銘柄抽出は `$TICKER` 形式の正規表現 + 大文字 1〜5 文字のホワイトリスト（`ticker_stats.txt` 由来。手動コピー）で行う。

### ルール定義 `rules/trading-rules.yaml`（初期値。根拠付きで確定、kyo は承認時に数値を変更できる）

**設計方針: 作戦（比率）は資金額に依存しない。資金額 `capital_usd` だけを kyo が決める。** 金額の上限（1 銘柄上限・日次/週次損失上限・DD 上限額）は
YAML に書かず、起動時に `capital_usd × pct` から `DerivedLimits` として導出する。paper と live で同じ YAML を使えば、資金額を変えても比率は同じ作戦として比較できる。

```yaml
approved_by: ""          # kyo が記入（trader rules approve 時に必須）
approved_at: ""

# 運用資金（USD）。paper でも live 予定額と同じ額にして、paper 成績と live 成績を同じ土俵で比較できるようにする。
# 既定 $500（約 7.5 万円）: 実弾で全額失っても生活に影響しない額、かつ 1 銘柄 15% = $75 で Serenity 銘柄（多くが $5〜$60）を
# 整数株で買える下限付近。
# kyo が承認時に変更できる。$300〜$1,000 の範囲なら以下の比率は変えなくてよい。$300 未満だと 1 銘柄 $45 未満になり
# 買える銘柄が減るので、position.max_concurrent_positions を 3 に減らすことを推奨（1 銘柄 25% になる）。
capital_usd: "500"

position:
  # capital の 15%。5 銘柄で 75% 稼働、残り 25% は現金バッファ（スリッページ・端数・翌日の追加買いの余地）。
  max_notional_per_ticker_pct: "15"
  # 分散と資金効率の折衷。増やすと 1 銘柄あたりが小さくなり、整数株で買える銘柄が減る。
  max_concurrent_positions: 5
  # 永久塩漬け防止の上限。Serenity のテーゼは 6〜12 ヶ月だが、結果を計測するために期限で強制クローズして記録する。
  # 120 日 = 約 4 ヶ月 = テーゼ期間の下限の 2/3。paper の数週間で到達する想定ではなく、live での安全弁。
  max_holding_days: 120

exit:
  # 対象銘柄は 1 日 15〜25% 動く。-8% 等の一般的な値はノイズで狩られる。-15% は「1 日の揺れの上限」に近く、
  # それを超えたらテーゼ崩れとみなす水準。1 ポジションの最大損失 = 1 銘柄上限 15% × 損切り 15% = capital の 2.25%。
  stop_loss_pct: "-15"
  # フォロワー殺到で急騰した分を半分確定し、残り半分でテーゼの 6〜12 ヶ月を追う。
  partial_take_profit_pct: "30"
  partial_take_profit_fraction: "0.5"
  # 部分利確後の残り半分に適用するトレーリングストップ（説明は本文）。20% は対象銘柄の日次変動幅より広く、
  # 1 日の揺れで狩られにくい値。
  trailing_stop_pct: "20"

loss_limits:
  # capital の 3%。1 ポジションの損切り 2.25% + スリッページ ≒ 1 回の損切り。同日 2 回目の損切りで止まる
  # （「悪い日」に損切りが連鎖するのを防ぐ）。
  max_daily_loss_pct: "3"
  # capital の 6%。週に損切り 2〜3 回で止まる。
  max_weekly_loss_pct: "6"
  # 口座全体の評価額（現金 + 保有評価額）が過去最高評価額から 15% 下がったら停止。
  # 日次・週次は「短期の連鎖」を止める基準、ドローダウンは「じわじわ負け続けている = 作戦が機能していない」を
  # 止める基準で役割が違う。15% = 週次上限 6% の 2.5 週分。
  max_drawdown_pct: "15"

entry:
  # 流動性の下限。薄い銘柄は cost_assumptions のスリッページ 0.5% が成り立たない。
  min_price_usd: "2"                   # ペニー株除外
  min_avg_daily_volume: 200000         # 直近 20 日平均の出来高（株）
  excluded_tickers: []

# R-23 実弾期待値の係数（決定値。live ブローカー確定時に commission_* と fx_cost_pct_one_way を合わせる）
cost_assumptions:
  # 小型株の成行注文の想定（片道）。entry.min_avg_daily_volume 20 万株が前提。
  slippage_pct_per_side: "0.5"
  # 約定代金に対する手数料率。Alpaca: "0" / moomoo: "0.132"（上限 $22、commission_cap_usd で表現）/ ウィブル: 要確認。
  commission_pct: "0"
  commission_cap_usd: ""               # 空 = 上限なし。moomoo なら "22"
  # 注文あたりの固定手数料。SEC/FINRA fee 等の実額は fills.fee に記録するのでここには含めない。
  commission_usd_per_order: "0"
  # 入出金時の USD/JPY コスト（片道、概算）。
  # - Alpaca（海外送金）: 銀行の固定送金手数料 数千円 + 為替スプレッド。capital $500 では固定手数料だけで 5% 以上に相当し、
  #   成績を食う。live を Alpaca にするのはこの点でも不利で、国内業者（円入金）が本命。
  # - 国内業者（円入金、為替スプレッド 25 銭/USD 程度）: 約 0.25%。
  # 初期値は両者の間の保守的な "1.0" を置く（paper 段階の概算用）。live 業者が決まったら実コストに合わせる。
  fx_cost_pct_one_way: "1.0"
```

**導出される絶対額（`DerivedLimits`。起動時に `capital_usd × pct` で計算、USD セントに量子化）**:

| 項目 | 計算 | capital $500（既定） | capital $2,000（旧既定、参考） |
|---|---|---|---|
| 1 銘柄上限 | capital × 15% | $75.00 | $300.00 |
| 損切り 1 回の最大損失 | 1 銘柄上限 × 15% | $11.25（= capital の 2.25%） | $45.00 |
| 日次損失上限 | capital × 3% | $15.00 | $60.00 |
| 週次損失上限 | capital × 6% | $30.00 | $120.00 |
| ドローダウン上限額 | peak_equity × 15%（peak の初期値 = capital） | $75.00 | $300.00 |
| 5 銘柄満杯時の稼働額 | 1 銘柄上限 × 5 | $375.00（現金バッファ $125） | $1,500.00 |

`entry_checks` / `loss_limits` はこの `DerivedLimits` を受け取って判定し、`rule_check_reason` には導出額と元の比率の両方を書く（kyo が後から読んで検算できるように）。

**整数株の制約**: v1 は**整数株のみ**。1 株の価格が 1 銘柄上限（$500 なら $75）を超える銘柄は `entry_checks` が「見送り」にする。
Alpaca は端株（fractional）を成行で扱えるが、live 候補の国内業者は端株非対応なので、paper でも整数株に統一して paper / live の成績を比較可能にする。
株数 = `floor(1 銘柄上限 / 現在値)`。

**トレーリングストップとは**: 買値ではなく、**買ってから付けた最高値**を基準にする損切り。最高値から X% 下がったら売る。
上がる間はストップ価格も一緒に上がる（下がることはない）ので、利益を伸ばしつつ、崩れたら利益を守れる。
例: $100 で買い → $150 まで上昇 → ストップは $120（$150 × 0.8）→ $120 まで下落したら売り。
$100 → $105 → $84 のように最高値をほとんど更新しないまま下げた場合は、通常の損切り -15%（$85）が先に効く。
本 spec での発動条件: **部分利確（+30%）を実行した後の残り半分に適用**する。それまでは `stop_loss_pct` が効く。
`positions.high_watermark` を毎サイクル更新し（下げない）、`exit_checks` が `high_watermark × (1 − trailing_stop_pct/100)` と現在値を比較する。

**ドローダウン判定の計算**: 毎サイクル、`equity = cash + Σ(保有数量 × 現在値)` を `equity_snapshots` に upsert し、
`peak_equity = max(前日までの peak_equity, equity)`、`drawdown_pct = (peak_equity − equity) / peak_equity × 100`。
`drawdown_pct >= max_drawdown_pct` でキルスイッチ（R-19）。`resume` は `halted` を解除するだけで `peak_equity` はリセットしない
（リセットすると「負け続けても毎回 15% ずつ許す」ことになる。kyo が意図的にリセットしたい場合はルールファイルの `capital_usd` を変えて `rules approve` する。
`capital_usd` を変えると `DerivedLimits` も全て再導出されるので、資金追加・減額の手続きはこれ 1 つに集約される）。

### データフロー

```
[手動] yan-labs data/*.json → data/sources/serenity/
   │  trader ingest
   ▼
sources/serenity/adapter (pydantic 検証, fail fast) → ledger.mentions
   │  trader run-cycle（cron 15 分間隔、市場時間内）
   ▼
engine/cycle:
  1. settings 検証（モード・キー）／ rules lock 検証／ RuleSet → DerivedLimits 導出／ engine_state.halted なら判断のみで終了
  2. ledger から保有ポジション・当日/当週損益を取得、口座評価額を equity_snapshots に記録
     → rules/loss_limits（日次・週次の導出額・ドローダウン %）→ 到達なら kill_switch
  3. 保有中: market data で現在値 → high_watermark 更新 → rules/exit_checks → ExitSignal なら decision(origin=rule_exit)
  4. 新規: sources/evidence（直近言及・新規言及銘柄）+ market data → analysis/analyst（LLM）→ Proposal
     → rules/entry_checks（DerivedLimits）→ decision(rule_check=passed|rejected)
  5. passed な decision → engine/approval（paper: system 承認 / live: kyo 承認レコード要、期限内）
  6. engine/order_executor: ledger に order(status=recorded) を書く → 成功時のみ broker.submit
     → status=submitted → ポーリングで fills → positions/trades 更新
  7. cycles に結果を記録
   │  trader report
   ▼
report/metrics + live_estimate → テキスト
```

### 証券会社の比較（R-14、Q2）

kyo の問い「Alpaca にした経緯は？ウィブル証券・moomoo証券はどう？」への回答。intent 作成時点では「paper 環境が口座開設なしで今日から使え、公式 SDK が成熟している」ことだけで Alpaca を置いていた。国内 2 社を含めて比較し直した。

| 軸 | Alpaca（米国） | ウィブル証券（Webull Japan） | moomoo証券（Japan） |
|---|---|---|---|
| paper と live が同一 API か | **同一**。URL 切替のみ。paper は口座開設不要・無料 | 日本法人の OpenAPI で paper が使えるかは**未確認**（グローバルの Webull Corp は 2026 年に paper 対応を発表） | **模擬取引（paper）対応を公式が明記** |
| 公式 SDK | `alpaca-py`（取引 + マーケットデータ。最も成熟） | `webull-python-sdk`（core / quotes / trade。2024-10 開始） | Python / Java / C# / C++ / JavaScript（2026-03 開始、国内では最も新しい） |
| 接続方式 | HTTPS 直接 | HTTPS 直接 | **ローカルに `OpenD` ゲートウェイプロセスを常駐**させ、そこ経由で接続（運用マシンに常駐プロセスが 1 つ増える） |
| 認証キーの運用負荷 | 低。キーに期限なし（自分でローテーション） | **API Key の既定有効期限 45 日**。自動運用ではローテーション運用が必要（切れると cron が止まる） | OpenD にログイン（口座 + パスワード + 取引パスワード）。キー期限の制約は無いが常駐プロセスの監視が要る |
| 利用開始手続き | paper: キー発行のみ。live: 口座開設（195+ カ国対応） | 口座開設 + OpenAPI 利用申請（審査あり）→ アプリ登録 → API Key 生成 | 口座開設 + OpenAPI 利用申請 |
| 米国株手数料 | $0 | 要確認（約定代金の一定率、上限あり） | 約定代金 × 0.132%（税込）、上限 $22。API 利用は無料 |
| 入金 | **海外送金**（銀行の送金手数料 数千円 + 為替スプレッド。**capital $500 では固定手数料だけで 5% 以上に相当**し、成績を食う） | 円で入金可 | 円で入金可 |
| 税務 | 海外業者なので**特定口座なし → 確定申告を自分でやる** | 特定口座（源泉徴収）が使える | 特定口座（源泉徴収）が使える |
| 端株（fractional） | 成行のみ対応（本 spec では使わない。整数株に統一） | 非対応 | 非対応 |
| 対応商品 | 米国株・ETF・（暗号資産） | 米国株現物・信用・オプション（買い）、日本株現物 | 米国株（API）。バックテスト・リアルタイムデータ（気配・板・ティック）も API で取れる |
| サポート | 英語 | 日本語 | 日本語 |
| API 実績年数 | 長い（2018〜） | 約 2 年 | 半年 |

**決定（2026-09-14 kyo）**:

- **v1（段階 (1) paper）は Alpaca**。理由: 今日から動かせる（口座開設・審査待ちなし）、paper = live 同一 API なので paper で検証したコードパスがそのまま live の土台になる（R-15〜R-17 のガードを同じコードで検証できる）、SDK が最も成熟している。
- **段階 (2) の live 移行時に、実口座を Alpaca / moomoo / ウィブルのどれにするか決め直す。国内 2 社が本命**。理由: 特定口座と円入金で個人には明確に有利。Alpaca は確定申告と海外送金コストが毎回かかり、capital $500 規模では入金コストが成績を食う。`Broker` インターフェースの背後に隠す設計（R-14）はそのためにある。live 移行時に `MoomooBroker` または `WebullBroker` を追加し、paper 成績の比較対象は Alpaca の記録をそのまま使う。
- ウィブルを live にする場合の注意: 日本法人の OpenAPI で paper が使えるか未確認、API Key 45 日期限のローテーション運用が要る。moomoo にする場合の注意: `OpenD` 常駐プロセスの監視が要る。いずれも live 移行の spec で扱う。
- IBKR は intent どおり差し替え候補として残すのみ（比較には含めない）。

以降の設計・AC は Alpaca 前提で書いてある。

### 外部インターフェース

| 相手 | 用途 | 認証 | 備考 |
|---|---|---|---|
| Alpaca Trading API（paper: `paper-api.alpaca.markets` / live: `api.alpaca.markets`） | 発注・注文照会・ポジション・口座 | モード別の trade キー | 発注はリトライなし、`client_order_id` で冪等。**整数株のみ**（Alpaca は端株を成行で扱えるが、live 候補の国内業者は端株非対応なので paper でも整数株に統一し、成績を比較可能にする）。1 株が 1 銘柄上限（capital × 15%）を超える銘柄は見送り |
| Alpaca Market Data API | 日足・分足・最新気配・市場カレンダー | モード別の read キー | 無料の IEX フィードで開始（Q7）。足りなければ別プロバイダを `MarketDataProvider` の実装追加で対応 |
| Anthropic API | 判断（構造化 JSON） | `ANTHROPIC_API_KEY` | temperature 低め、モデル ID は設定。ツール呼び出しは与えない（読み取り専用の入力だけ） |
| yan-labs アーカイブ | 集約データ | なし（ファイル） | 手動コピー。ネットワーク取得はしない |
| （段階 (2) 以降）moomoo OpenAPI / ウィブル OpenAPI | live 発注 | OpenD ログイン / API Key | 本 spec では実装しない。`Broker` 実装を追加する |

### エラー処理

- 各層は `Result` を返す。CLI が `Err` を人間向けメッセージ + exit code 非 0 に変換する
- 起動時エラー（キー不足・lock 不一致・DB 不整合・`capital_usd` が正の Decimal でない・pct が 0〜100 の範囲外）は**何もせず終了**
- サイクル内の部分失敗: LLM 失敗 → その銘柄は `skip` として記録し続行。マーケットデータ失敗 → 売り判定も評価額計算もできないので**新規買いをしない**（保有は次サイクルで再評価。`equity_snapshots` はそのサイクルでは更新しない）。ブローカー送信失敗 → order を `failed` にして理由を記録、同一 `client_order_id` で再送はしない
- DB 書き込み失敗 → 発注しない（R-16）。サイクルを `outcome=error` で終了
- 例外は境界（外部 SDK 呼び出し）で捕捉して `Err` に変換する。空 `except` 禁止

### 秘密情報

- `.env.example` に変数名だけ記載。`.env*` は `.gitignore` 済み・Claude からの読み取り deny 済み
- `Settings` はモードごとに別クラス（`PaperSettings` / `LiveSettings`）。paper では live 変数を定義しないため読み込まれない
- ログ出力は `SecretStr` の repr（`**********`）のみ。記録 DB にキーを保存しない
- Alpaca のキーは read 用・trade 用を別発行し、read キーで発注しないことを `AlpacaBroker` の構築時に検証する（trade キーが無ければ `Broker` を構築できない型設計）

## スコープ外

- 段階 (3)(4): 「確実な条件は自動」の判定ロジック、承認不要の live 発注
- live 口座の開設・入金・初回 live 発注の実施（土台は作るが、実施は paper 成績を見て kyo が別途判断）
- live 用ブローカー実装の追加（`MoomooBroker` / `WebullBroker` / IBKR）。live 移行の spec で扱う
- 日本株、楽天証券
- ニュース・決算の自動解析、Serenity 以外の情報源の実装（`SourceAdapter` の差し替え余地のみ）
- 発信者の方法論・テーゼの自作、yan-labs の `references/*.md` や `SKILL.md` の取り込み（本 spec は `data/*.json` のみ）
- 集約データの自動更新（yan-labs パイプライン停止時の自前取得、Q8）
- Web UI・通知（Slack 等）。`report` はテキスト出力のみ
- 指値・逆指値の注文種別（v1 は市場時間内の成行のみ。ルールによる売りはエンジン管理）
- 端株（fractional）の発注
- 為替の自動処理、税務計算

## 受入基準

前提: `uv` が導入済みで `uv sync` 済み。全 verify は `TRADER_ENV=test` 相当（テスト内で強制）で実ネットワークに出ない。

- [ ] AC-1: リポジトリが推奨スタックで初期化され、lint・format チェックが通る
  verify: uv run ruff check . && uv run ruff format --check .
- [ ] AC-2: mypy strict で `src/` に型エラーがない（`Any` は境界のみ）
  verify: uv run mypy src
- [ ] AC-3: unit テストが全て通り、カバレッジ 80% 以上
  verify: uv run pytest tests/unit -q --cov=src --cov-fail-under=80
- [ ] AC-4: 集約データ（serenity tweets.json 形式）を取り込み、スキーマ違反レコードがあれば件数付きで拒否し何も保存しない（R-1, R-2）
  verify: uv run pytest tests/unit/sources/test_serenity_adapter.py -q
- [ ] AC-5: 情報源は `SourceAdapter` プロトコル経由で追加でき、`serenity` 以外のダミーアダプタを登録して取り込める（R-3）
  verify: uv run pytest tests/unit/sources/test_adapter_registry.py -q
- [ ] AC-6: 銘柄別の言及履歴（初出日・直近言及日・言及回数・抜粋）が mentions から導出される（R-4）
  verify: uv run pytest tests/unit/sources/test_evidence.py -q
- [ ] AC-7: LLM 出力は pydantic スキーマで検証され、スキーマ違反・ルール変更を含む出力・不明銘柄は判断として採用されない（R-5, R-7）
  verify: uv run pytest tests/unit/analysis/test_output_schema.py -q
- [ ] AC-8: LLM の「買い」提案がルールエンジンを通過しない限り注文候補にならず、ルールエンジンを迂回する発注 API が存在しない（R-6）
  verify: uv run pytest tests/unit/engine/test_no_bypass.py -q
- [ ] AC-9: ルールファイルの SHA-256 が lock と不一致なら起動を拒否し、`rules approve` 実行後のみ lock が更新される（R-10）
  verify: uv run pytest tests/unit/rules/test_lock.py -q
- [ ] AC-10: ロードした RuleSet は frozen で、属性代入や LLM モジュール経由の変更が例外になる。`analysis/` 配下はルールファイル・lock への書き込みコードを含まない（R-11）
  verify: uv run pytest tests/unit/rules/test_immutable_rules.py -q && ! grep -rEn "open\(.*rules|trading-rules" src/trader/analysis/
- [ ] AC-11: 買い前検証（対象外銘柄 / 同時保有数 / 1 銘柄上限 = capital × pct / 既保有 / 損失上限 / 市場時間）の各条件で拒否理由付きの Err が返り、上限額が `capital_usd` と pct から Decimal で正しく導出される（capital $500 → 1 銘柄 $75.00、日次 $15.00、週次 $30.00）（R-9, R-12）
  verify: uv run pytest tests/unit/rules/test_entry_checks.py tests/unit/rules/test_derived_limits.py -q
- [ ] AC-12: 損切り・部分利確・トレーリングストップ・保有期間上限の各条件で正しい ExitSignal が返り、LLM を呼ばずに売り decision が作られる（R-13）
  verify: uv run pytest tests/unit/rules/test_exit_checks.py -q
- [ ] AC-13: `TRADER_MODE` 省略時は paper。live は live キー + `TRADER_LIVE_CONFIRM` が揃わないと起動拒否。`TRADER_ENV=test` では条件が揃っても live を拒否する（R-15, N-2）
  verify: uv run pytest tests/unit/config/test_mode_guard.py -q
- [ ] AC-14: paper モードの Settings は live 用環境変数を読まず、AlpacaBroker はモードから決まる URL 以外を受け付けない（N-3）
  verify: uv run pytest tests/unit/config/test_key_separation.py tests/unit/broker/test_alpaca_urls.py -q
- [ ] AC-15: 判断・承認・注文レコードの DB 書き込みが失敗した場合、Broker.submit が呼ばれない（R-16）
  verify: uv run pytest tests/unit/engine/test_record_before_submit.py -q
- [ ] AC-16: paper では system 承認が自動付与され、live では kyo の有効な承認レコードが無い注文は送信されない。期限切れ承認も拒否される（R-17）
  verify: uv run pytest tests/unit/engine/test_approval.py -q
- [ ] AC-17: 約定ポーリングで filled / partially_filled / canceled / rejected が fills と orders.status に正しく記録される（R-18）
  verify: uv run pytest tests/unit/ledger/test_fills.py -q
- [ ] AC-18: 日次・週次（capital × pct の導出額）・ドローダウン（%）のいずれかの損失が上限に達すると、未約定注文が全キャンセルされ、新規発注が停止し、`halted` が永続化され、`resume` まで次サイクルでも発注しない（R-19）
  verify: uv run pytest tests/unit/engine/test_kill_switch.py -q
- [ ] AC-19: 全 decision に evidence 参照・LLM モデル ID・プロンプト/レスポンスのハッシュ・rule_set_sha256・rule_check 結果が記録され、見送り・拒否も残る（R-20）
  verify: uv run pytest tests/unit/ledger/test_decision_record.py -q
- [ ] AC-20: ポジションを閉じたとき、根拠・売却理由（発火ルール or LLM）・実現損益・手数料・保有日数を持つ trade が 1 行作られる（R-21）
  verify: uv run pytest tests/unit/ledger/test_trade_closer.py -q
- [ ] AC-21: report の指標（期間損益・勝率・平均損益・最大ドローダウン・判断内訳）が既知のデータセットに対して期待値と一致する（R-22）
  verify: uv run pytest tests/unit/report/test_metrics.py -q
- [ ] AC-22: 実弾期待値がスリッページ・手数料・為替の係数を差し引いて算出され、係数はルールファイルから読まれる（R-23）
  verify: uv run pytest tests/unit/report/test_live_estimate.py -q
- [ ] AC-23: `src/trader/{domain,broker,ledger,rules}` に float 型注釈・`float()` 呼び出しが存在しない（N-1）
  verify: uv run python scripts/check_no_float_money.py
- [ ] AC-24: 発注 POST はリトライされず、GET はバックオフ付きリトライされ、全外部呼び出しにタイムアウトがある（N-4）
  verify: uv run pytest tests/unit/broker/test_retry_policy.py tests/unit/analysis/test_llm_client.py -q
- [ ] AC-25: Alpaca レスポンス・環境変数・ルール YAML の不正入力（絶対額キー `*_usd` の混入、pct の範囲外、`capital_usd` が非正を含む）が境界で拒否され、人間が読めるメッセージ（キー値・スタックトレースを含まない）になる（N-5, N-6）
  verify: uv run pytest tests/unit/config/test_settings_validation.py tests/unit/broker/test_response_validation.py tests/unit/rules/test_schema_validation.py -q
- [ ] AC-26: domain モデルは全て frozen で、属性代入が例外になる（N-7）
  verify: uv run pytest tests/unit/domain/test_immutability.py -q
- [ ] AC-27: `src/` 配下に 800 行を超えるファイルがない（N-8）
  verify: ! find src -name '*.py' -exec awk 'END{if(NR>800){print FILENAME; exit 1}}' {} \; | grep -q .
- [ ] AC-28: 統合テスト: FakeBroker + 実 SQLite で ingest → run-cycle（買い、capital $500 で整数株）→ 価格更新 → run-cycle（損切り売り）→ report まで一気通貫で動き、trade が 1 件記録される（R-8, R-24）
  verify: uv run pytest tests/integration/test_paper_cycle.py -q
- [ ] AC-29: CLI サブコマンド `ingest / run-cycle / approve / report / rules approve / resume / status` が存在し `--help` が exit 0 で返る（R-24）
  verify: for c in ingest run-cycle approve report "rules approve" resume status; do uv run trader $c --help >/dev/null || exit 1; done
- [ ] AC-30: `.env.example` に N-2 の変数名が全て列挙され、値が空である。`.gitignore` が `.env` `data/` `var/` を含む
  verify: for v in ALPACA_PAPER_READ_KEY ALPACA_PAPER_TRADE_KEY ALPACA_LIVE_TRADE_KEY ANTHROPIC_API_KEY TRADER_MODE TRADER_LIVE_CONFIRM; do grep -qE "^$v=$" .env.example || exit 1; done && grep -qx 'data/' .gitignore && grep -qx 'var/' .gitignore
- [ ] AC-31: `CLAUDE.md` の Commands セクションに `TODO` が残っていない（スタック確定の反映）
  verify: ! awk '/^## Commands/{f=1;next} /^## /{f=0} f' CLAUDE.md | grep -q TODO
- [ ] AC-32: Alpaca paper 口座に実際に接続してサイクルが完走し、判断と注文が記録される（実キー必要のため Deploy 後に kyo が `trader run-cycle` と `trader report` で確認）
  verify: true
- [ ] AC-33: `trader report` の出力が kyo にとって「何を根拠に買い、なぜ売り、いくら儲かったか」を読める形になっている（Deploy 後に人間が確認）
  verify: true

## 未解決の問い（intent から引き継ぎ・ここで解いたもの）

- Q1: yan-labs の導入方法 → **解決**: `npx skills add` / `skills update` は使わない（第三者 CLI の安全性未確認、自己更新指示は「内容をレビューなしで差し替えない」制約に反する）。`data/aleabitoreddit_tweets.json` と `ticker_stats.txt` だけを `data/sources/serenity/` に手動コピーし、スキーマ検証して取り込む。`SKILL.md` と `references/*.md` は取り込まない（方法論はプロンプトに人間が要約して入れる運用。本 spec では扱わない）。ライセンス未設定のため再配布しない（git 管理外）
- Q2: 証券会社 → **解決（2026-09-14 kyo 確定）**: v1（paper）は Alpaca、段階 (2) の live 移行時に Alpaca / moomoo / ウィブルから決め直す（国内 2 社が本命）。比較は「設計 > 証券会社の比較」。Alpaca を live で使う場合の注意: 海外送金の入金コスト（capital $500 規模では固定手数料だけで 5% 以上に相当）と特定口座なし（確定申告が必要）。IBKR は `Broker` 差し替え余地として残す
- Q3: 売買ルール → **初期値を根拠付きで確定（「ルール定義」の YAML コメント参照）。全て資金に対する割合で定義し、kyo は承認時に `capital_usd` と比率を変更できる**。要点: capital 既定 $500（実弾で失っても生活に影響しない額）/ 1 銘柄 15%（$75）/ 同時 5 / 損切り -15%（1 ポジション最大損失 = capital の 2.25% = $11.25）/ 部分利確 +30% で半分 / 残りにトレーリング -20% / 保有上限 120 日 / 日次 -3%（$15）週次 -6%（$30）/ 口座全体ドローダウン -15%（$75）。整数株のみ。paper の記録で「損切り発火回数」「ドローダウン推移」「1 株が上限超えで見送った回数」を見て調整する
- Q4: AI の判断根拠 → **plan で prompt.py の具体に落とす**: evidence として「直近 14 日の新規言及銘柄」「言及回数の増減」「直近の言及抜粋」を渡し、株価データとして「直近 20 日の日足」「出来高平均」を渡す。買いのトリガーは LLM に任せるが、見送り条件（既保有・上限・対象外・流動性）はルールで機械判定。重み付けはデータが溜まるまで固定しない
- Q5: 段階移行条件 → **方向性を確定（実装は別 spec）**: paper → 少額実弾は「paper 3 週間以上 + 閉じた trade 10 件以上 + 実弾期待値がプラス + 最大 DD が weekly 上限未満 + キルスイッチが誤作動していない」。的中率の確定を待たない
- Q6: 「確実」の機械判定 → **引き続き未解決（段階 (3) の spec で決める）**。本 spec は decision に `origin` / `confidence` / `rule_check` を残すので、後から条件別の成績を集計できる
- Q7: 株価データ → **解決**: Alpaca Market Data API（無料 IEX フィード）で開始。日足・分足・最新気配・市場カレンダーが取れれば v1 は足りる。`MarketDataProvider` の実装追加で差し替え可能（live 移行時に国内業者にする場合はその相場 API）
- Q8: 集約データの更新 → **引き続き未解決（運用で決める）**: v1 は手動コピーの再取り込み（`ingest` は冪等。`external_id` で重複排除）。自前取得はスコープ外
- Q9: 発信者の追加基準 → **引き続き未解決（運用で決める）**。提案: 「構造化アーカイブが存在する / 監査済み / 6 ヶ月以上の実績」。`SourceAdapter` 追加で対応
- Q10: 実弾期待値 → **解決**: R-23。`実弾期待値 = paper 損益 − Σ(約定金額 × slippage_pct_per_side) − Σ min(約定金額 × commission_pct, commission_cap_usd) − 注文数 × commission_usd_per_order − capital × fx_cost_pct_one_way × 2` を report に並べる。係数は `cost_assumptions` に決定値を記載（slippage 0.5% / commission は Alpaca 前提で 0 / fx は保守的に 1.0%。live 業者確定時に合わせる: 国内業者なら fx 0.25%、Alpaca なら $500 規模で 5% 以上）

### 確定事項（2026-09-14 kyo 回答）

- 承認の有効期限: 次の市場セッション終了まで（R-17）
- `run-cycle` の実行間隔: 市場時間内 15 分（R-8, N-10）
- Q5 の移行条件の方向性: 上記のとおり
- cost_assumptions の値: AI が決定（Q10）
- 証券会社: (a) v1 は Alpaca、live 移行時に決め直す（国内 2 社が本命）（R-14, Q2）
- 資金: 実弾 $2,000 は怖い → ルールを比率で定義し直し、`capital_usd` 既定を $500 に変更（Q3）

### kyo が承認時に確定するもの（まとめ）

1. 技術スタック: Python 3.12 + uv（推奨 A）
2. スコープ: 段階 (1) 完成 + 段階 (2) の土台まで
3. `capital_usd` の額（既定 $500。$300〜$1,000 なら比率はそのままでよい）と、ルール比率を変えるかどうか（変えなければ「ルール定義」の初期値で `rules approve` する）

## 承認

approved_by:
approved_at:
