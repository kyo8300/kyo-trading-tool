---
name: plan
description: ハーネス第3段階。承認済み spec.md から実装計画 specs/<feature>/plan.md（変更ファイル / 作業順序 / リスク / Proof）を書き、人間承認を得る。/plan <feature> と呼ばれたとき、または spec が承認された直後に使う。
argument-hint: <feature-name>
---

# /plan — 実装計画（人間介入ポイント 3/3・最後の介入点）

`$ARGUMENTS` を feature 名として扱う。

## 前提チェック

1. `specs/<feature>/spec.md` が存在し `approved_by:` が埋まっていること。未承認なら `/spec <feature>` を案内して終了。
2. `specs/<feature>/plan.md` が承認済みなら「`/harness <feature>` で自律ループを開始できます」と伝えて終了。

## 手順

1. `planner` サブエージェント（model: fable）に委譲する。渡すもの:
   - `specs/<feature>/spec.md`, `intent.md` のパス
   - テンプレート `specs/_template/plan.md` のパス
   - コードベースを読むこと（既存パターン・テスト構成）。編集はしない。
2. planner は次を書く:
   - 変更ファイル（新規 / 変更 / 削除を明示）
   - 作業順序（`T-1`, `T-2`, … のタスク ID 付き。各タスクは 1 commit で完結する粒度。依存関係と、独立して並列実行できるタスクの組を明記）
   - リスク（既知の制約・レート制限・互換性）
   - Proof（各受入基準 AC-n をどのテスト / 動作で証明するかの対応表）
3. 要点をユーザーに提示する。特に次を強調する:
   - **これが自律ループ前の最後の介入点**であること
   - タスク数と並列可能な組
   - 人間確認が必要な受入基準（`verify: true` のもの）
4. 承認の案内:

   ```
   承認するなら plan.md 末尾の承認欄を埋めてください:
     approved_by: <あなたの名前>
     approved_at: YYYY-MM-DD
   承認後は /harness <feature> で自律ループが始まります。
   ```

5. 修正要望があれば planner に差し戻す。

## 禁止事項

- spec.md に無い作業を計画に入れない。
- コードを書かない。
