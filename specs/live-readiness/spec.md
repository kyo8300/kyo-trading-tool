# Spec: live-readiness（paper → 少額実弾の移行条件をテーゼの時間軸に合わせて作り直す）

- 元 intent: `specs/live-readiness/intent.md`（approved_by: kyo, approved_at: 2026-10-06）
- 親 spec: `specs/ai-auto-trader/spec.md`（Q5 / R-22 / R-23 を変更対象とする。R-番号・N-番号・AC-番号は親 spec のもの）
- 作成日: 2026-10-06
- 状態: draft

> **kyo へ**: intent で「spec で詰める」とされた 7 点（6 週の起点 / コスト控除後を判定に使うか / 「上回る」の最小マージン / SPY 配当 / close の decision origin / close の実行タイミング / SPY 価格の取得元）は「設計判断」で推奨案を決めて理由を書いた。
> 本当に kyo の判断が要るものだけ「kyo 確認事項」に 5 つ列挙した（推奨付き。承認時に「推奨どおり」で済む）。

## 概要

paper 運用のルール（損切り -15% / 部分利確 +30% / 保有上限 120 日）は 6〜12 ヶ月のテーゼ前提で、数週間では trade が閉じない。親 spec Q5 の「閉じた trade 10 件以上」はこれと噛み合わず、数ヶ月〜1 年満たせない。本 spec は移行条件を **「口座全体の損益率（実現 + 評価）を同じ期間の SPY と比べる」** 形に置き換え、`trader report` に判定を並べて表示し、売り経路の未検証を `trader close <ticker>` で埋める。

### この spec が満たす範囲

| 項目 | 内容 |
|---|---|
| 移行条件の定義 | intent「望む結果」の条件 1〜5 を機械計算可能な形に落とす（親 spec Q5 の置き換え） |
| 計器 | `trader report` に「移行条件」セクションを追加: ポートフォリオ損益率（実現 + 評価、コスト控除後も併記）と SPY リターンを並べ、各条件の 満たした / 未達 / 未判定 / 要確認 を表示 |
| SPY データ | `run-cycle` が毎サイクル SPY の日足終値を記録する（既存の Alpaca Market Data 経路に 1 銘柄増やすだけ） |
| 銘柄別の評価損益の記録 | `run-cycle` が毎サイクル保有銘柄の時価（mark）を記録する（「最も儲かった 1 銘柄を除く」判定と、起点日からの損益計算のため） |
| `trader close <ticker>` | kyo が指定した保有銘柄を全株売る手動 close。decision（origin=`manual`）→ 承認 → 注文 → 約定 → trade 記録まで、ルール発火の売りと**同じコード経路**を通す |
| 親 spec の更新 | `specs/ai-auto-trader/spec.md` の Q5 を本 spec への参照に更新する |

### 満たさない範囲

live 口座の開設・入金・ブローカー選定・`MoomooBroker` / `WebullBroker`・初回 live 発注、売買ルール（yaml の比率・`capital_usd`）の変更、AI の判断ロジック、段階 (3)(4) の自動化条件、自動での「合格」宣言やモード切替。詳細は「スコープ外」。

## 技術スタック

CLAUDE.md 準拠（Python 3.12 + uv + pytest + ruff + mypy strict + pydantic v2 + alpaca-py + sqlite3）。**新しい依存は追加しない**。SPY の日足は既存の `alpaca-py` `StockHistoricalDataClient`（IEX フィード、read キー）で取る。

## 機能要件

番号は本 spec 固有の `LR-n`。親 spec の要件は `R-n` / `N-n` で参照する。

### 移行条件の定義（intent 望む結果 1〜4、制約「後出しで甘くしない」「同じ期間・同じ起点」）

- LR-1: 移行条件は以下の 5 条件（intent の番号と対応）とし、`trader report` がそれぞれを **満たした / 未達 / 未判定（データ不足）/ 要確認（kyo の判断が必要）** の 4 値で表示する。「合格」の宣言・live への切り替えはしない（Q6）
  1. **期間**: 起点日 `S` から評価日 `E` まで **42 日（6 週間）以上**
  2. **SPY 超過**: ポートフォリオ損益率 `P` − SPY リターン `B` ≥ **+1.00 ポイント**（`P`, `B` の定義は LR-2〜LR-4）
  3. **偏り対策**: 最も儲かった 1 銘柄の損益（正の場合のみ）を除いた損益率 `P_ex` ≥ `B`（マージンなし。intent の「SPY 以上」）
  4. 親 spec から引き継ぎ: (a) 実弾期待値（コスト控除後、LR-5）> 0 / (b) capital 基準の最大ドローダウン（LR-6）< `max_weekly_loss_pct`（現行 6%）/ (c) キルスイッチ発火回数（LR-7）= 0。発火 ≥ 1 回は「要確認」とし、誤作動かどうかは kyo が判断する
  5. **売り経路の検証**: `trader close` による paper の売りで閉じた trade（`exit_reason = manual`、exit decision の `mode = paper`）が **1 件以上**
- LR-2: **起点日 `S`** の既定は **paper 開始日 = `equity_snapshots` の最古の `snapshot_date`**（データから導出し、人が選ばない）。`trader report --since YYYY-MM-DD` で `S` を後ろにずらせる（前にはずらせない。ずらした場合は `S` 時点の mark が必要で、無ければ条件 2・3 は「未判定」）。**評価日 `E`** = `equity_snapshots` の最新の `snapshot_date`（最後に `run-cycle` が走った ET 日）
- LR-3: **ポートフォリオ損益率 `P`**（%）= `(PnL(E) − PnL(S)) / capital_usd × 100`。`PnL(d)` = `Σ trades.realized_pnl（closed_at の ET 日 ≤ d）` + `Σ position_marks(d).unrealized_pnl`。`PnL(S)` は起点日の mark が無ければ 0 とみなす（paper 開始日は保有ゼロなので正確）。分母は常にルールファイルの `capital_usd`（Alpaca paper 口座の現金残高ではない。Q4「判定は %」と、paper 口座の現金が capital と一致しない可能性への対処）。銘柄別の損益 `PnL_t` も同じ式で求め、`P_ex = (PnL(E) − PnL(S) − max(0, max_t PnL_t)) / capital_usd × 100`
- LR-4: **SPY リターン `B`**（%）= `(close(E) / close(S) − 1) × 100`。終値は `benchmark_prices` から取り、`S` の行が無ければ `S` 以降 5 日以内の最初の行、`E` の行が無ければ `E` 以前の最新の行を使い、代用したことを表示する。**配当は含めない**（価格リターン。理由は設計判断 D-4）。5 日以内に行が無ければ「未判定」
- LR-5: **コスト控除後**の損益 = `report/live_estimate.estimate(paper_pnl = PnL(E) − PnL(S), fills = 期間内の fills, order_count = 期間内の注文数, capital, cost_assumptions)` の `estimated_live_pnl`。条件 4(a) の判定に使い、`P` と並べて `P_net`（%）としても表示する。**条件 2・3 の判定は `P`（コスト控除前）で行う**（理由は D-2）
- LR-6: **capital 基準の最大ドローダウン**（%）= `max over snapshot_date ≥ S of (running_peak_equity − equity) / capital_usd × 100`（`equity_snapshots` の `equity` を使い、`S` 以降のランニング最高値から測る）。親 spec の `max_drawdown_pct`（peak 比）とは別に算出し、両方表示する
- LR-7: **キルスイッチ発火回数** = `cycles` のうち `outcome = 'halted'` かつ `error_summary` が `'engine is halted'` 以外（= 損失上限到達でその場で止めたサイクル）の行数（`started_at` の ET 日 ≥ `S`）
- LR-8: 判定に使う数値は全て `Decimal`。% は小数 2 桁に `ROUND_HALF_EVEN` で量子化してから比較・表示する（親 N-1）

### 計器（intent 望む結果「並んで表示」、R-22 / R-23 の変更）

- LR-9: `trader report` の出力に「## 移行条件（paper → 少額実弾）」セクションを追加する。内容: 起点日 / 評価日 / 経過日数、`P`（内訳: 実現・評価・capital）、`P_net`、`B`（`close(S) → close(E)`、「配当含まず」）、条件 1〜5 の各行（判定 + 根拠数値）、総合行（未達・未判定・要確認の件数と番号、「最終判断は kyo」）。既存セクションは変更しない。`--from/--to` はこのセクションに影響しない（常に `S`〜`E`）
- LR-10: データ不足（`benchmark_prices` が空、mark が無い、snapshot が無い）でも `report` は exit 0 で既存セクションを出し、該当条件を「未判定」+ 不足しているデータ名で表示する（`run-cycle` が 1 回走れば埋まる旨を添える）
- LR-11: 既存の「## ルール発火回数(売却理由別)」に `manual` が自然に現れる（`ExitReason.manual` の追加による。render の変更は不要）

### SPY と mark の記録（intent 影響範囲「SPY の価格データの取得」）

- LR-12: `MarketDataProvider` に `daily_bars_since(ticker, start: date) -> Result[tuple[Bar, ...], MarketError]` を追加する。`AlpacaMarketData` は `StockBarsRequest(start=start)` で取り、既存の `_to_bar`（pydantic 検証）を通す。`FakeMarketData` は `bars` を `date ≥ start` で絞って返す
- LR-13: `run-cycle` は `market_clock` 取得成功後（データフロー 2 の前）に、`daily_bars_since("SPY", S)` を 1 回呼び、全バーを `benchmark_prices` に upsert する（`S` は LR-2 の既定。`equity_snapshots` が空なら当日）。終値 ≤ 0 のバーは記録せず警告にする。**失敗はサイクルを止めない**: `CycleOutcome.benchmark_error` に理由を入れ、CLI が 1 行表示する
- LR-14: `engine/holdings.evaluate_holdings` は価格を取れた保有銘柄ごとに `position_marks`（`mark_date` = ET 日, `ticker`, `qty`, `avg_cost`, `mark_price`, `unrealized_pnl`, `taken_at`）を upsert する。ポジションが閉じたとき（`engine/fills._settle_fill` が `delete_position` する箇所）は同日の `position_marks` 行も削除する（実現損益との二重計上防止）

### `trader close <ticker>`（intent 影響範囲、制約「売りの処理の確認を落とさない」「close は手動の売りであり、記録される」）

- LR-15: CLI `trader close TICKER [--reason TEXT]` を追加する。保有中の `TICKER` を**全株**成行で売る。`--reason` は decision の `rationale` に `manual close by kyo: <reason>` として残る（省略時は `manual close by kyo`）。`TRADER_ENV=test` では `run-cycle` と同様に実行を拒否する（テストは engine 関数を直接呼ぶ。N-9）
- LR-16: `domain.models.Origin` に `manual`、`ExitReason` に `manual` を追加する。close の decision は `action=sell, origin=manual, rule_check=passed, rule_check_reason="manual close (sell of held position; entry rules do not apply)", proposed_notional=現在値×株数, reference_price=現在値, llm_model/prompt_sha256/response_sha256=None`
- LR-17: close は **`run-cycle` と同じ関数**で承認・発注・約定処理を行う: `cycle._execute_passed_decisions` を `engine/execution.execute_decisions(...)` に切り出し、`run_cycle` と `close_position` の両方がそれだけを呼ぶ。`resolve_approval` / `order_executor.execute` / `poll_and_settle` を直接呼ぶのは `engine/execution.py` のみ（静的検査で固定）。`submit_market_order` の呼び出し元は引き続き `order_executor.py` のみ（親 AC-8）
- LR-18: close の前提検証（満たさなければ何も記録せず Err、人間向けメッセージ）: (a) `TICKER` の `positions` 行がある (b) `engine_state.halted` でない（halted 中は `trader resume` を先に実行する。R-19 の「新規発注停止」と整合）(c) 市場時間内（`market_clock.is_open`。v1 は市場時間内の成行のみ）(d) `latest_price(TICKER)` が取れる
- LR-19: close は `run-cycle` と同じロックファイルを取り（同時実行防止、N-10）、`cycles` に `id = close_<uuid>` の行を作ってから decision を書く（`decisions.cycle_id` の FK を満たし、`status` の「last cycle」にも現れる）
- LR-20: 承認は `resolve_approval` に委ねる。paper では `approver=system` が自動付与されて即時発注・約定ポーリング・`trade_closer` まで 1 回の実行で完了する。**live では既存の有効な `approver=kyo` 承認が無ければ発注せず**、decision id と `trader approve <id> --expires-at ...` → `trader close TICKER` の再実行を案内して exit 1 する。再実行時は**同じ ET 日**に作られた未発注（`orders` 行なし）の manual decision を再利用する（別日なら新しい decision を作る。R-17 を迂回しない）
- LR-21: 約定後は `trade_closer` が `exit_reason = manual` の `Trade` を 1 行作り、`positions` の行は削除される。5 枠に空きができ、次サイクルで新規買いが走りうる（Q7。close の出力にその旨を 1 行表示する）
- LR-22: close の出力: decision id / order id / 約定株数・平均約定価格 / trade id / 実現損益。部分約定・タイムアウト時は `orders.status` と約定済み株数を表示し、残りは次の `run-cycle` のポーリング対象にならないことを明記する（既存の `poll_and_settle` の挙動と同じ。v1 の限界として記録）

### 親 spec の更新・運用

- LR-23: `specs/ai-auto-trader/spec.md` の Q5 の行を「→ **2026-10-06 改訂**: 移行条件は `specs/live-readiness/spec.md` を正とする（『閉じた trade 10 件以上』は廃止。旧記述は履歴として残す）」の形に更新する。元の文は削除せず打ち消し表現で残す
- LR-24: CLI サブコマンド一覧（親 R-24）に `close` を追加し、`--help` が exit 0 で返る

## 非機能要件

- NFR-1: 金額・株数・価格・% は `Decimal` のみ（親 N-1）。`src/trader/{domain,broker,ledger,rules}` の float 検査（親 AC-23）は本 spec 後も通る。`report/readiness.py` も float を使わない（検査対象ディレクトリ外だが規約として守る）
- NFR-2: SPY のバーは境界で pydantic 検証する（既存 `_RawBar`。親 N-5）。終値 ≤ 0 は記録しない
- NFR-3: 新しい外部呼び出しは `daily_bars_since` の GET 1 回/サイクルのみ。既存の `RetryPolicy(3, 0.5, 4)` + タイムアウト 10 秒が適用される（親 N-4）。1 年分でも ≤ 260 行で N-10（5 分以内）に影響しない
- NFR-4: `benchmark_prices` / `position_marks` の読み書きは全てパラメータ化クエリ（security.md）。upsert は冪等
- NFR-5: 全データ構造は frozen（親 N-7）。`Position` への追加はしない（mark は別テーブルに持つ）
- NFR-6: `report` は**ネットワークに出ない**（DB とルールファイルだけで完結する。移行条件の数値は `run-cycle` が記録したものだけを使う）
- NFR-7: テストは `TRADER_ENV=test` で実ネットワークに出ない（親 N-9）。`close` の engine 関数は `FakeBroker` / `FakeMarketData` / `FixedClock` で検証する
- NFR-8: 1 ファイル 400 行目安・800 行上限（親 N-8）。`cli.py` は現在 649 行なので、`close` の本体は `cli.py` に足さず `engine/manual_close.py` に置き、`cli.py` にはルーティングと依存組み立てだけを置く。`run-cycle` と `close` の依存組み立て（settings → broker / market 構築）は `cli.py` 内の共通関数に切り出して重複を避ける
- NFR-9: 既存の DB（本番 `var/trader.sqlite3`）に対してマイグレーション `0002` が無停止で適用される（新テーブルの追加のみ。既存テーブルは変更しない）

## 設計

### 設計判断（intent「spec で詰める」項目の決定）

| ID | 問い | 決定 | 理由 |
|---|---|---|---|
| D-1 | 6 週の起点 | **paper 開始日**（`equity_snapshots` の最古日。`--since` で後ろにだけずらせる） | 人が選ばない数値で「後出し」の余地が無い。ルールは paper 開始前に lock 済みなので全期間が正当な記録。SPY も同じ日から取れる（`daily_bars_since`）。条件決定日（2026-10-06）を起点にする選択肢は `--since 2026-10-06` で kyo が選べる（ただしその日の mark が必要なので、本 spec が 10-06 以降に deploy されると「未判定」になる） |
| D-2 | コスト控除後を判定に使うか | **条件 2・3 はコスト控除前 `P`、条件 4(a) はコスト控除後** | `fx_cost_pct_one_way 1.0% × 2 = capital の 2%` は入出金 1 往復の固定費で、6〜12 ヶ月のテーゼ全体で償却するもの。6 週間の窓に全額載せると SPY 比較が構造的に不可能になる。一方「実弾期待値プラス」（条件 4a）は親 spec から引き継ぐので、コストは**そこで**効く。両方表示するので kyo はどちらも見られる |
| D-3 | 「上回る」の最小マージン | **条件 2 は +1.00 ポイント、条件 3 は 0** | 1.00pt の内訳: SPY 配当の無視分（6 週で約 0.15〜0.3pt、D-4）+ 往復スリッページ（0.5% × 2 × 稼働率 75% ≈ 0.75pt）。intent は条件 3 を「SPY 以上」と書いているのでそのまま 0。現状の数字（+3.5〜17.9% の含み益、SPY は未取得）に寄せた値ではなく、コスト構造から導いた |
| D-4 | SPY 配当（Q5） | **含めない（価格リターン）** | SPY の分配は四半期ごと（3/6/9/12 月第 3 金曜前後）で年率約 1.2%、6 週分は 0.14% 程度。含めると SPY 側のハードルが上がる（= 厳しくなる）方向なので、無視する分は D-3 のマージンで吸収する。配当込みにするには分配データの取得経路が別途要る（IEX 日足には無い）ので、依存を増やさない |
| D-5 | close の decision origin | **`Origin.manual` + `ExitReason.manual`** | decisions.origin（`llm` / `rule_exit` / `manual`）で「誰が決めたか」、trades.exit_reason（`stop_loss` 等 / `manual`）で「なぜ売ったか」が既存の report でそのまま区別できる。新しい列やテーブルは不要 |
| D-6 | close の実行タイミング（Q7） | **即時実行**（`trader close` を打った時点で、市場時間内なら承認 → 発注 → 約定まで完了。live は承認を挟んで再実行） | 「次サイクルで実行」方式は決定の有効期限管理（市場カレンダー依存）と、kyo が結果を見に行く手間が増える。即時なら kyo がその場で `trader report` を打って条件 5 を確認できる。どの銘柄をいつ売るかは kyo の判断（intent Q7）。推奨: 含み益が部分利確 +30% から遠い銘柄を 1 つ、寄り付き直後を避けた時間帯に |
| D-7 | SPY 価格の取得元 | **Alpaca Market Data（IEX 日足）を `run-cycle` が毎サイクル `benchmark_prices` に記録し、`report` は DB だけを読む** | `report` を非ネットワークのまま保てる（テスト容易、offline で読める）。1 サイクル 1 GET で backfill も自動（起点日から全部 upsert するので cron が止まった日の穴も次に埋まる）。別プロバイダ追加や手動 CSV は依存・手順が増える |
| D-8 | 損益の分母 | **常に `capital_usd`**（口座 equity ではない） | 現行 report の最大 DD 0.06% は、保有 5 銘柄 ≈ $1,875 の日次変動から見て Alpaca paper 口座の現金が capital $2,500 より大きい（既定 $100k）可能性を示唆する。equity 比の % は口座残高で薄まるので、paper と live の % を比較するには capital 比にする必要がある（intent「判定は % で行い … 同じ条件で読める」）。既存のキルスイッチの DD 判定（peak 比）は本 spec では触らない（スコープ外、kyo 確認事項 4） |

### 構成（新規 / 変更）

```
src/trader/
├── cli.py                          # 変更: close コマンド追加、report に --since、run-cycle/close 共通の依存組み立て関数
├── domain/models.py                # 変更: Origin.manual, ExitReason.manual
├── market/
│   ├── data_provider.py            # 変更: daily_bars_since を Protocol に追加
│   ├── alpaca_data.py              # 変更: daily_bars_since 実装（StockBarsRequest(start=...)、既存 _to_bar 検証）
│   └── fake_data.py                # 変更: daily_bars_since 実装
├── ledger/
│   ├── migrations/0002_live_readiness.sql   # 新規: benchmark_prices, position_marks
│   └── benchmark_repository.py     # 新規: benchmark_prices / position_marks の upsert・list・delete（パラメータ化のみ）
├── engine/
│   ├── execution.py                # 新規: execute_decisions（cycle._execute_passed_decisions の移設。approval → executor → fills の唯一の経路）
│   ├── benchmark.py                # 新規: record_benchmark(conn, market, start, now) → Result。失敗は Err を返すが呼び元は止めない
│   ├── manual_close.py             # 新規: close_position(deps, ticker, reason) → Result[CloseOutcome, CloseError]
│   ├── cycle.py                    # 変更: execution.execute_decisions を使う、benchmark 記録を呼ぶ、CycleOutcome.benchmark_error
│   ├── holdings.py                 # 変更: 価格取得後に position_marks を upsert
│   └── fills.py                    # 変更: delete_position と同時に当日の position_marks を削除
└── report/
    ├── readiness.py                # 新規: 純粋関数 evaluate_readiness(inputs) → Readiness（条件 1〜5 の判定）
    ├── readiness_inputs.py         # 新規: DB → ReadinessInputs（起点日・評価日・trades・marks・benchmark・snapshots・cycles・fills の読み出し）
    ├── render.py                   # 変更: 移行条件セクションの描画（render_readiness）
    └── __init__.py                 # 変更: export 追加

tests/
├── unit/domain/test_manual_origin.py
├── unit/ledger/test_migration_0002.py
├── unit/ledger/test_benchmark_repository.py
├── unit/market/test_daily_bars_since.py
├── unit/engine/test_benchmark.py
├── unit/engine/test_marks.py
├── unit/engine/test_manual_close.py
├── unit/engine/test_shared_execution_path.py
├── unit/report/test_readiness.py
├── unit/report/test_readiness_inputs.py
├── unit/report/test_render_readiness.py
├── unit/test_cli.py                # 変更: COMMANDS に close、close の test 環境拒否、report に移行条件セクション
└── integration/test_manual_close_cycle.py
```

### データモデル（追加のみ。`0002_live_readiness.sql`）

| テーブル | カラム | 備考 |
|---|---|---|
| `benchmark_prices` | `ticker` TEXT, `price_date` TEXT（ET 日 ISO）, `close` TEXT（canonical Decimal）, `taken_at` TEXT, PK(`ticker`, `price_date`) | `run-cycle` が `daily_bars_since("SPY", S)` の全バーを upsert。当日分はサイクル時点の「ここまでの終値」で、翌日以降のサイクルで確定値に上書きされる |
| `position_marks` | `mark_date` TEXT, `ticker` TEXT, `qty` TEXT, `avg_cost` TEXT, `mark_price` TEXT, `unrealized_pnl` TEXT, `taken_at` TEXT, PK(`mark_date`, `ticker`) | `evaluate_holdings` が価格を取れた保有ごとに upsert。ポジションが閉じたら同日の行を削除。`unrealized_pnl = (mark_price − avg_cost) × qty` を cent に量子化 |

既存テーブルの変更なし。`decisions.origin` / `trades.exit_reason` は TEXT で CHECK 制約が無いので `manual` の追加に DDL は不要。

### 読み出しの定義（`readiness_inputs.py` → `readiness.py`）

```
S  = --since ?? min(equity_snapshots.snapshot_date)          # 無ければ「未判定」
E  = max(equity_snapshots.snapshot_date)
realized(d)     = Σ trades.realized_pnl where trading_day(closed_at) <= d
realized_t(d)   = 同、ticker 別
unreal(d)       = Σ position_marks.unrealized_pnl where mark_date = d   # 行が無ければ 0（d = S のとき）/ d = E で保有があるのに行が無ければ「未判定」
unreal_t(d)     = 同、ticker 別
PnL(d)          = realized(d) + unreal(d)
window_pnl      = PnL(E) − PnL(S)
best_ticker_pnl = max(0, max_t (realized_t(E) + unreal_t(E) − realized_t(S) − unreal_t(S)))
P      = window_pnl / capital × 100
P_ex   = (window_pnl − best_ticker_pnl) / capital × 100
P_net  = live_estimate.estimate(window_pnl, fills in (S, E], orders in (S, E], capital, cost).estimated_live_pnl / capital × 100
B      = (benchmark close(E) / close(S) − 1) × 100          # 代用規則は LR-4
DD_cap = max over snapshots with date >= S of (running_peak − equity) / capital × 100
kills  = count(cycles where outcome='halted' and error_summary <> 'engine is halted' and trading_day(started_at) >= S)
manual = count(trades where exit_reason='manual' and (exit decision の mode)='paper')
```

判定（% は 2 桁量子化後に比較）:

| 条件 | 満たした | 未達 | 未判定 / 要確認 |
|---|---|---|---|
| 1 | `E − S ≥ 42 日` | それ未満 | `S` が無い → 未判定 |
| 2 | `P − B ≥ 1.00` | それ未満 | `B` または `unreal(E)` が無い → 未判定 |
| 3 | `P_ex ≥ B` | それ未満 | 同上 |
| 4a | `P_net の元の estimated_live_pnl > 0` | `≤ 0` | 条件 2 と同じ |
| 4b | `DD_cap < max_weekly_loss_pct` | `≥` | snapshot が無い → 未判定 |
| 4c | `kills = 0` | – | `kills ≥ 1` → 要確認（件数と各 `error_summary` を表示） |
| 5 | `manual ≥ 1` | `0` | – |

### データフロー

```
trader run-cycle（既存）
  1. 起動検証 … 2. market_clock
  2'. [新規] engine/benchmark.record_benchmark: daily_bars_since("SPY", S) → benchmark_prices upsert（失敗は benchmark_error に記録して続行）
  3. holdings: latest_price → high_watermark → [新規] position_marks upsert → exit_checks → rule_exit decision
  4〜6. 既存（candidates → approval → execution.execute_decisions → fills。閉じたら position_marks の当日行を削除）

trader close TICKER（新規）
  lock 取得 → halted 確認 → positions 確認 → market_clock（open 必須）→ latest_price
  → cycles(close_<uuid>) 行 → 同日の未発注 manual decision を再利用 or 新規 decision(origin=manual) 挿入
  → engine/execution.execute_decisions（resolve_approval → order_executor.execute → poll_and_settle → trade_closer）
  → paper: trade(exit_reason=manual) まで完了 / live: 承認なしなら decision id を案内して exit 1

trader report [--since]（変更）
  既存の metrics / live_estimate / trades … + [新規] readiness_inputs(DB) → readiness.evaluate → render_readiness
```

### 外部インターフェース

| 相手 | 用途 | 変更 |
|---|---|---|
| Alpaca Market Data API（IEX） | SPY 日足（`daily_bars_since`） | GET が 1 サイクル 1 回増える。read キーのみ。リトライ・タイムアウトは既存 |
| Alpaca Trading API（paper） | close の売り注文 | 既存の `order_executor` 経由。新しい呼び出し種別なし |

### エラー処理

- `record_benchmark` の失敗（Alpaca 障害・検証失敗）: `Err` を返すが `run_cycle` は続行し、`CycleOutcome.benchmark_error` に人間向け理由を入れる（SPY は計器であり、売買判断に使わないため）
- `position_marks` の upsert 失敗: `evaluate_holdings` が `Err` → サイクルは `outcome=error`（既存の upsert_position 失敗と同じ扱い。記録できないものは進めない）
- `close` の前提違反（LR-18）: 何も書かずに `Err`。メッセージは「`TICKER` を保有していません」「エンジンが halted です（trader resume）」「市場時間外です（次の open: …）」「価格を取得できません」。キー値・パス・スタックトレースを含めない（N-6）
- `close` の発注失敗: `order_executor` が `orders.status=failed` + `last_error` を書く（既存）。close は exit 1 で理由を表示し、再送しない（N-4）
- `report` のデータ不足: exit 0、該当条件を「未判定」（LR-10）。`--since` が `S` より前 / 日付形式不正は exit 1 + `YYYY-MM-DD`
- 既存 DB の `0002` 適用失敗: `open_db` が `Err`（既存のマイグレーション機構）

## スコープ外

- live 口座の開設・入金・ブローカー選定・`MoomooBroker` / `WebullBroker` の実装・初回 live 発注・`TRADER_MODE=live` での close の実運用確認（次の intent。本 spec は live の承認経路を unit テストで通すところまで）
- `rules/trading-rules.yaml` の比率・`capital_usd` の変更、yaml への新キー追加（lock の再承認が要るので触らない）
- キルスイッチの DD 判定（peak 比）を capital 比に変える変更（kyo 確認事項 4 で別 intent にするか決める）
- SPY 以外の比較対象（半導体 ETF、Serenity 言及銘柄バスケット）。`benchmark_prices.ticker` を列に持つので後から 1 銘柄足せる設計にはしておく
- 配当込みの SPY トータルリターン
- `close` の部分売り（`--qty`）・指値・市場時間外の予約
- `status` への移行条件の表示（`report` のみ）
- Web UI・通知

## 受入基準

前提: `uv sync` 済み。全 verify は `TRADER_ENV=test` 相当（`tests/conftest.py` が強制）で実ネットワークに出ない。

- [ ] AC-1: lint・format チェックが通る
  verify: uv run ruff check . && uv run ruff format --check .
- [ ] AC-2: mypy strict で `src/` に型エラーがない
  verify: uv run mypy src
- [ ] AC-3: unit テストが全て通り、カバレッジ 80% 以上
  verify: uv run pytest tests/unit -q --cov=src --cov-fail-under=80
- [ ] AC-4: `src/trader/{domain,broker,ledger,rules}` に float が無い（NFR-1）。`report/readiness.py` が存在し、`float` という識別子が無い
  verify: uv run python scripts/check_no_float_money.py && test -f src/trader/report/readiness.py && ! grep -nE '\bfloat\b' src/trader/report/readiness.py
- [ ] AC-5: `Origin.manual` と `ExitReason.manual` が存在し、`decisions` / `trades` に書いて読み戻せる（LR-16, D-5）
  verify: uv run pytest tests/unit/domain/test_manual_origin.py -q
- [ ] AC-6: マイグレーション `0002` で `benchmark_prices` と `position_marks` が作られ、`0001` だけ適用済みの既存 DB に対しても既存行を壊さず適用される（NFR-9）
  verify: uv run pytest tests/unit/ledger/test_migration_0002.py -q
- [ ] AC-7: `benchmark_prices` / `position_marks` の upsert は冪等で、同一キーの再書き込みは上書きになり、閉じた銘柄の当日 mark を削除できる。SQL は全てパラメータ化（NFR-4）
  verify: uv run pytest tests/unit/ledger/test_benchmark_repository.py tests/unit/ledger/test_sql_safety.py -q
- [ ] AC-8: `daily_bars_since` が Protocol・Alpaca 実装・Fake 実装に揃い、Alpaca 実装は `start` を `StockBarsRequest` に渡し、不正バー（負値・OHLC 不整合）を Err にし、GET リトライ・タイムアウトが効く（LR-12, NFR-2, NFR-3）
  verify: uv run pytest tests/unit/market/test_daily_bars_since.py tests/unit/market/test_alpaca_data.py -q
- [ ] AC-9: `run_cycle` が毎サイクル SPY の日足を起点日から `benchmark_prices` に upsert し、終値 ≤ 0 のバーは記録せず、Alpaca 失敗時はサイクルを止めずに `CycleOutcome.benchmark_error` に理由を残す（LR-13）
  verify: uv run pytest tests/unit/engine/test_benchmark.py -q
- [ ] AC-10: `evaluate_holdings` が価格を取れた保有ごとに `position_marks` を upsert し（`unrealized_pnl = (mark − avg_cost) × qty`）、ポジションが閉じると同日の mark 行が消える（LR-14）
  verify: uv run pytest tests/unit/engine/test_marks.py -q
- [ ] AC-11: `close_position` が paper で「decision(origin=manual, rule_check=passed) → system 承認 → order → fill → trade(exit_reason=manual)」を 1 回で完了し、`positions` の行が消え、`cycles` に `close_` 行が残る（LR-15〜LR-21）
  verify: uv run pytest tests/unit/engine/test_manual_close.py -q -k "paper_happy_path"
- [ ] AC-12: `close_position` は 未保有 / halted / 市場時間外 / 価格取得失敗 のそれぞれで何も記録せず Err（人間向けメッセージ、キー値・パスを含まない）になる（LR-18）
  verify: uv run pytest tests/unit/engine/test_manual_close.py -q -k "refuses"
- [ ] AC-13: live モードでは kyo の有効な承認が無い限り `submit_market_order` が呼ばれず decision id が案内され、同日の再実行は同じ decision を再利用し、有効な承認があれば発注される。期限切れ承認では発注されない（LR-20, R-17）
  verify: uv run pytest tests/unit/engine/test_manual_close.py -q -k "live"
- [ ] AC-14: `run_cycle` と `close_position` は承認・発注・約定処理を `engine/execution.execute_decisions` だけ経由で行う: `resolve_approval` / `order_executor.execute` / `poll_and_settle` の呼び出しが `src/trader/engine/execution.py` 以外に無く、`submit_market_order` の呼び出しは `order_executor.py` のみ（LR-17, 親 AC-8）
  verify: uv run pytest tests/unit/engine/test_shared_execution_path.py tests/unit/engine/test_no_bypass.py -q
- [ ] AC-15: 既存のサイクル挙動（部分失敗・halted・市場時間外・損切り売り）が変わらない（リグレッション）
  verify: uv run pytest tests/unit/engine/test_cycle.py tests/unit/engine/test_kill_switch.py tests/unit/engine/test_candidates.py -q
- [ ] AC-16: `evaluate_readiness` が既知の入力に対して条件 1〜5 の判定を期待どおり返す: 42 日境界（41 日=未達 / 42 日=満たした）、`P − B` が 0.99pt=未達 / 1.00pt=満たした、最大 1 銘柄除外（正の最大だけ除く・全銘柄マイナスなら除かない）、`P_net` の算出、DD の分母が capital、キルスイッチ ≥ 1 回=要確認、manual close 0 件=未達、データ不足=未判定。全て `Decimal` で 2 桁量子化（LR-1〜LR-8）
  verify: uv run pytest tests/unit/report/test_readiness.py -q
- [ ] AC-17: `readiness_inputs` が DB から 起点日（最古 snapshot / `--since`）・評価日・期間内の実現損益（銘柄別）・評価日の mark・SPY 終値（代用規則を含む）・capital 基準 DD・キルスイッチ回数・manual close 件数 を正しく読み出す（LR-2〜LR-7）
  verify: uv run pytest tests/unit/report/test_readiness_inputs.py -q
- [ ] AC-18: `report` の出力に「移行条件」セクションがあり、SPY リターンとポートフォリオ損益率（`P` / `P_net`）が並び、条件 1〜5 の各行に 満たした / 未達 / 未判定 / 要確認 のいずれかと根拠数値が付き、総合行に「最終判断は kyo」とあり、「合格」「live に切り替え」等の自動宣言文言が無い。データ不足でも exit 0（LR-9, LR-10, Q6）
  verify: uv run pytest tests/unit/report/test_render_readiness.py -q
- [ ] AC-19: 既存の report 指標（親 AC-21 / AC-22）が変わらない
  verify: uv run pytest tests/unit/report/test_metrics.py tests/unit/report/test_live_estimate.py tests/unit/report/test_render.py -q
- [ ] AC-20: CLI `close` が存在し、`ingest / run-cycle / approve / report / rules approve / resume / status / close` の `--help` が exit 0。`close` は `TRADER_ENV=test` で実行を拒否し、`report --since` の不正日付は exit 1 で `YYYY-MM-DD` を案内する（LR-15, LR-24）
  verify: for c in ingest run-cycle approve report "rules approve" resume status close; do uv run trader $c --help >/dev/null || exit 1; done && uv run pytest tests/unit/test_cli.py -q
- [ ] AC-21: 統合テスト: FakeBroker + 実 SQLite で run_cycle（買い）→ run_cycle（SPY と mark が記録される）→ `close_position`（manual 売り、trade 1 件）→ `trader report` で条件 5 が「満たした」、SPY とポートフォリオの損益率が表示される → 次の run_cycle で空いた枠に新規買いが走る（LR-13, LR-14, LR-21, Q7）
  verify: uv run pytest tests/integration/test_manual_close_cycle.py -q
- [ ] AC-22: 既存の統合テストが通る（リグレッション）
  verify: uv run pytest tests/integration/test_paper_cycle.py -q
- [ ] AC-23: `src/` 配下に 800 行を超えるファイルがなく、`cli.py` に `close` の業務ロジック（`resolve_approval` / `execute_decisions` / `poll_and_settle` の直接呼び出し）が無い（NFR-8）
  verify: ! find src -name '*.py' -exec awk 'END{if(NR>800){print FILENAME; exit 1}}' {} \; | grep -q . && ! grep -nE 'resolve_approval|execute_decisions|poll_and_settle' src/trader/cli.py
- [ ] AC-24: 親 spec の Q5 が本 spec を指し、改訂日が入っている（LR-23）
  verify: grep -q 'specs/live-readiness/spec.md' specs/ai-auto-trader/spec.md && awk '/^- Q5:/{print; exit}' specs/ai-auto-trader/spec.md | grep -q '2026-10-06'
- [ ] AC-25: Deploy 後に kyo が確認: 市場時間内に `run-cycle` が 1 回走ったあと `trader report` の移行条件セクションに SPY リターンと保有 5 銘柄込みの損益率が表示され、各条件の判定が読める（人間が確認）
  verify: true
- [ ] AC-26: Deploy 後に kyo が確認: paper で `trader close <ticker>` を 1 回実行し、`trader report` のトレード一覧に `manual` の trade が 1 件、条件 5 が「満たした」になる。空いた枠で次サイクルに新規買いが走ることを `status` で確認する（人間が確認）
  verify: true

## 未解決の問い（intent から引き継ぎ・ここで解いたもの）

- Q1〜Q4: intent で決定済み。本 spec はそれに従う（6 週 + 偏り対策 / 手動 close / 範囲 / capital のずれ承知）
- Q5: SPY の配当 → **解決（D-4）**: 含めない。無視分はマージン（D-3）で吸収
- Q6: 合格判定の運用 → **解決（LR-1, LR-9, AC-18）**: report が各条件の判定を表示するだけ。自動宣言・モード切替は行わず、テストで「合格」文言が無いことを検査する。承認時に再確認（kyo 確認事項 5）
- Q7: close の実行タイミング → **解決（D-6）**: 即時実行。どの銘柄をいつ売るかは kyo。推奨は D-6 に記載。枠が空くことは close の出力に明記

## kyo 確認事項（承認時に「推奨どおり」でよい）

1. **6 週の起点（D-1）**: 推奨 = paper 開始日（`equity_snapshots` 最古日、2026-09-18 頃）。代替 = 条件決定日 2026-10-06（`report --since 2026-10-06`。ただし 10-06 の mark が無いと条件 2・3 は未判定）
2. **マージンと判定基準（D-2, D-3）**: 推奨 = 条件 2 は `P − B ≥ 1.00pt`（コスト控除前）、条件 3 は `P_ex ≥ B`、コスト控除は条件 4(a) で効かせる。代替 = 条件 2 もコスト控除後 `P_net` で判定（6 週では構造的に厳しい）
3. **SPY 配当（D-4）**: 推奨 = 含めない（価格リターン）
4. **損益率・DD の分母（D-8）**: 推奨 = `capital_usd`（$2,500）。確認したい事実: Alpaca paper 口座の現金残高は $2,500 に合わせてあるか（既定 $100k のままなら、既存のキルスイッチ DD 判定（peak 比 15%）が実質効いていない。これは本 spec のスコープ外なので、別 intent にするかどうかを決めてほしい）
5. **Q6 の再確認**: report は判定を表示するだけで、live への切り替え・合格宣言は一切しない。これでよいか

## 承認

approved_by:
approved_at:
