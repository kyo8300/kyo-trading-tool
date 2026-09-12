---
name: harness
description: ハーネス第4段階（自律ループ）。承認済み plan.md に従い builder → tester → reviewer を spec.md の受入基準を全て満たすまで回す。人間の介入なしで完了させる。/harness <feature> と呼ばれたときに使う。
argument-hint: <feature-name>
---

# /harness — 自律ループ（Build → Test → Review）

`$ARGUMENTS` を feature 名として扱う。ここから先、halted になるまで人間に質問しない。

## 前提チェック

- `specs/<feature>/plan.md` が `approved_by:` 済みであること。未承認なら `/plan <feature>` を案内して終了。
- 作業ブランチ: `main` 上なら `feat/<feature>` を作成して切り替える。

## ① 状態ファイルの初期化

`.claude/harness.local.json` を次の内容で作成する（既に `running` のものがあれば iteration 等を引き継ぐ）:

```json
{"feature":"<feature>","status":"running","iteration":0,"max_iterations":8,"review_rejections":0,"max_review_rejections":5,"session_id":"<$CLAUDE_SESSION_ID が取れなければ空文字>"}
```

## ② ゴール設定

`/goal` を次の条件で設定する:

> specs/<feature>/spec.md の全受入基準の verify コマンドが exit 0 で通り、かつ reviewer の verdict が APPROVE である

## ③ ループ本体

plan.md のタスク `T-n` を順序どおりに処理する。各タスクについて:

1. **builder**（Agent ツール、`builder` サブエージェント）
   - 渡す: feature 名、タスク ID と内容、`spec.md` / `plan.md` のパス、前回 REJECT の findings（あれば）
   - 期待: 実装 + conventional commit。テストの書き換えは禁止。
2. **tester**（`tester` サブエージェント）
   - 渡す: feature 名、タスク ID、対応する受入基準 AC-n
   - 期待: 失敗テスト → 実装確認 → 全体テスト実行。失敗があれば builder に戻す（これは Review 差し戻しには数えない）。
3. 全タスク完了後、**reviewer**（`reviewer` サブエージェント、読取専用）
   - 渡す: feature 名、`REVIEW.md`、`spec.md` / `plan.md`、`git diff main...HEAD`
   - 期待: verdict `APPROVE` / `REJECT`（findings に file:line）/ `HALT`

**並列化**: plan.md で「独立して並列実行可」と明記されたタスク群があり、かつユーザーが ultracode / Workflow 利用を明示している場合のみ、`.claude/workflows/build-test.js` を Workflow ツールで `args: {feature, tasks:[...]}` として実行する。それ以外は Agent ツールで直列に回す。

## ④ verdict の処理

| verdict | 処理 |
|---|---|
| `APPROVE` | 全 AC の `verify:` を実行。全て exit 0 なら状態を `status: "done"` にして完了報告。失敗があれば該当 AC を builder に渡して ③ へ |
| `REJECT` | `review_rejections` を +1。5 を超えたら `status: "halted"` にして人間へ。そうでなければ findings を builder に渡して ③ へ |
| `HALT` | `status: "halted"` にし、reviewer の理由（plan の前提が崩れている等）をそのまま人間へ報告して停止 |

## ⑤ Stop hook との関係

`.claude/hooks/verify-spec.sh` は Stop 時に `harness.local.json` が `running` なら全 `verify:` を実行し、未達があればセッション終了をブロックして未達 AC を返す（`iteration` が `max_iterations`=8 に達したら `halted` にして人間へ返す）。つまり途中で終了しようとしても受入基準を満たすまで戻される。`done` / `halted` のときはブロックしない。

## 完了報告（done / halted 共通）

- feature 名、最終 verdict、iteration / review_rejections の回数
- 受入基準の合否一覧（`verify: true` のものは「要・人間確認」と明記）
- commit 一覧（`git log main..HEAD --oneline`）
- halted の場合は原因と、人間に決めてほしいこと

## 禁止事項

- halted 以外で人間に質問しない。
- 受入基準の `verify:` コマンドやテストを緩めて通す。
- 状態ファイルを手で `done` にする（verify が通った結果としてのみ done にする）。
