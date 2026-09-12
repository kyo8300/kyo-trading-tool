# specs/ — ハーネスの成果物

機能ごとに `specs/<feature>/` を作り、3つのファイルを**この順に**人間が承認していく。承認後はAIが自律ループで完成させる。

```
[人間]  /intent <feature>  ── AI と討論 → intent.md（何を・なぜ）        → 承認
[人間]  /spec   <feature>  ── intent から spec.md（要件・設計・受入基準） → 承認
[人間]  /plan   <feature>  ── spec から plan.md（変更ファイル・順序・Proof）→ 承認  ← 最後の介入点
[自動]  /harness <feature> ── builder → tester → reviewer を受入基準を満たすまでループ
[自動]  deploy             ── production は hook で人間承認が必須
[人間]  成果チェック
```

## 各ファイルの役割

| ファイル | 誰が書く | 内容 | 人間が見るポイント |
|---|---|---|---|
| `intent.md` | intent-writer (Fable) と討論 | 問題 / 望む結果 / 影響範囲 / 制約 / 未解決の問い | 作る価値があるか、意図は合っているか |
| `spec.md` | spec-writer (Fable) | 概要 / 要件 / 設計 / **受入基準** | intent 通りか、設計・制約に無理がないか |
| `plan.md` | planner (Fable) | 変更ファイル / 作業順序 / リスク / Proof | 順序・リスク・証明方法に納得できるか |

テンプレートは `_template/` にある。

## 承認の仕方

各ファイル末尾の承認欄を埋めて commit する。埋まっていなければ次の段階のスキルは進まない。

```
## 承認
approved_by: kyo
approved_at: 2026-09-12
```

修正したいときは承認欄を空のままにして、会話で修正点を伝える（AI が差し戻して更新する）。

## 受入基準の書式（spec.md）

Stop hook が機械的に検証するので形式は厳密に守る:

```
- [ ] AC-1: ログイン後にダッシュボードが表示される
  verify: npm test -- --grep "dashboard"
- [ ] AC-2: チャートの見た目が承認済みモックと一致する（Deploy 後に人間が確認）
  verify: true
```

- `verify:` は2スペースインデント、1行のシェルコマンド、exit 0 で合格。
- 自動検証できないものは `verify: true` にし、基準文に「人間が確認」と書く。

## 自律ループの止まり方

- **done**: 全 `verify:` が通り、reviewer が APPROVE。
- **halted**: 次のいずれか。人間が判断して再開する。
  - Review 差し戻しが 5 回を超えた
  - Stop hook のブロックが 8 回に達した
  - reviewer が HALT（plan の前提が崩れている）と判定した

状態は `.claude/harness.local.json`（git 管理外）にある。
