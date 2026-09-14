# kyo-trading-tool

> **このRepoでの優先順位**: グローバル `~/.claude/rules` にある planner / tdd-guide / code-reviewer 等の
> エージェント名への指示は、このRepoでは**無効**。ワークフローは本ファイルの `## Harness` セクションが正。
> コーディング規約は `.claude/rules/*.md` を参照する。

## 概要

個人用トレーディングツール（AI が集約データを読み、人間が決めた売買ルールの中で米株を売買する。詳細は `specs/ai-auto-trader/spec.md`）。
技術スタックは `specs/ai-auto-trader/spec.md` で決定済み: **Python 3.12 + uv + pytest + ruff + mypy(strict) + pydantic v2 + alpaca-py + anthropic + sqlite3**。

## Commands

<!-- Stop hook (verify-spec.sh) はここではなく spec.md の verify: を実行する。ここは人間とエージェントが手で回すコマンド -->

- Build: `uv sync`
- Test: `uv run pytest tests/unit -q`（unit）/ `uv run pytest tests/integration -q`（integration）
- Lint: `uv run ruff check . && uv run ruff format --check .`
- Typecheck: `uv run mypy src`
- テスト実行時は `TRADER_ENV=test` が強制され、実ネットワークに出ない（spec N-9）

正常時の出力例（builder/tester が「通った」を判断する基準。初回の実装後に実際の出力へ差し替える）:

```
$ uv run ruff check . && uv run ruff format --check .
All checks passed!
N files already formatted
$ uv run mypy src
Success: no issues found in N source files
$ uv run pytest tests/unit -q
N passed in X.XXs
```

## Conventions

詳細は `.claude/rules/` を参照。要点:

- **イミュータブル**: 既存オブジェクトを変更せず、常に新しいオブジェクトを返す
- **小さいファイル**: 200〜400行が目安、800行を上限。機能/ドメイン単位で分割
- **TypeScript の場合**: `any` 禁止。interface / type を定義する
- **エラー処理**: すべての層で明示的に扱う。黙って握りつぶさない。UI向けは人間が読めるメッセージ
- **入力検証**: システム境界（ユーザー入力・外部API・ファイル）でスキーマ検証。fail fast
- **秘密情報**: ハードコード禁止。環境変数経由。起動時に必須値を検証
- **コミット**: Conventional Commits（feat / fix / refactor / docs / test / chore / perf / ci）。`/commit`（commit-commands プラグイン）を使ってよい

## Harness

人間が介入するのは **定義（intent / spec / plan の承認）** と **Deploy 後の成果チェック** のみ。
その間は AI が spec.md の受入基準を満たすまで自律的にループする。

```
[人間]  /intent <feature> ── intent-writer と討論 → specs/<feature>/intent.md → 人間承認
[人間]  /spec   <feature> ── spec-writer が要件・設計・受入基準 → spec.md      → 人間承認
[人間]  /plan   <feature> ── planner が変更ファイル/順序/リスク/Proof → plan.md  → 人間承認
[自動]  /harness <feature> ┐ builder  → タスク単位で実装 + commit
                           │ tester   → TDD、受入基準の verify 実行
                           │ reviewer → REVIEW.md の3パス。APPROVE / REJECT(builderへ) / HALT(人間へ)
                           └ Stop hook: 受入基準が全部通るまで終了をブロック（/goal 併用）
[自動]  deployer ── Deploy 先は未定。production は hook で人間承認必須
[人間]  成果チェック
```

| 段階 | エージェント | モデル | 入力 | 出力 |
|---|---|---|---|---|
| Intent | `intent-writer` | fable | 人間との討論 | `specs/<feature>/intent.md` |
| Spec | `spec-writer` | fable | 承認済み intent.md | `specs/<feature>/spec.md` |
| Plan | `planner` | fable | 承認済み spec.md | `specs/<feature>/plan.md` |
| Build | `builder` | sonnet | plan.md のタスク | コード + commit |
| Test | `tester` | sonnet | 実装 + spec.md | テスト + 実行結果 |
| Review | `reviewer` | opus（読取のみ） | diff + REVIEW.md + spec/plan | verdict: APPROVE / REJECT / HALT |
| Deploy | `deployer` | sonnet | 承認済み成果 | デプロイ（production は `RELEASE_APPROVAL` 必須） |

### 成果物と承認

- 成果物は `specs/<feature>/{intent,spec,plan}.md`。人間承認は各ファイルの承認欄に `approved_by: <name>` と日付を書いて commit する。未承認なら次段階の skill は進まない
- `spec.md` の `## 受入基準` は Stop hook がパースする厳密形式:
  ```
  - [ ] AC-1: <人間が読める基準>
    verify: <exit 0 で合格となるシェルコマンド>
  ```

### ループ制御

- 状態ファイル: `.claude/harness.local.json`（gitignore 済み）
  `{"feature","status":"running|done|halted","iteration","max_iterations":8,"review_rejections","max_review_rejections":5,"session_id"}`
- Stop hook (`.claude/hooks/verify-spec.sh`): status が `running` の間、受入基準の verify を全部実行し、失敗があれば終了をブロックして失敗一覧を返す。上限 **8 回**で `halted`
- Review 差し戻し: reviewer の REJECT は builder に戻す。上限 **5 回**で `halted`
- `halted` になったら AI は止まり、人間が判断する。plan.md の前提が崩れたと reviewer が判断した場合（HALT）も同様
- builder はテストを書き換えて通すことを禁止（テストは tester の領分）

### 補助プラグイン（公式）

- `commit-commands`: `/commit` でコミット作成
- `pr-review-toolkit`: reviewer が silent-failure / type-design / test-analyzer の観点を参考にする

## 人間の介入ポイント

| タイミング | 人間がやること |
|---|---|
| intent.md 承認 | 「作る価値があるか / 意図は合っているか」 |
| spec.md 承認 | 「intent 通りか / 設計・スタック・制約に問題ないか」 |
| plan.md 承認 | 「変更範囲・順序・Proof が妥当か」 |
| halted / HALT | ループが止まった理由を読み、plan に戻すか中止するか決める |
| production deploy | `RELEASE_APPROVAL` を設定して明示的に許可する |
| Deploy 後 | 成果を実際に触って確認 |

@specs/README.md
