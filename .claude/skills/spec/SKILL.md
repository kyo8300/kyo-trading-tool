---
name: spec
description: ハーネス第2段階。承認済み intent.md から要件・設計・受入基準を specs/<feature>/spec.md に書き、人間承認を得る。/spec <feature> と呼ばれたとき、または intent が承認された直後に使う。
argument-hint: <feature-name>
---

# /spec — 要件・設計書（人間介入ポイント 2/3）

`$ARGUMENTS` を feature 名として扱う。

## 前提チェック（満たさなければ進まない）

1. `specs/<feature>/intent.md` が存在し、承認欄 `approved_by:` が埋まっていること。
   - 未承認なら「intent が未承認です。`/intent <feature>` で承認を済ませてください」と伝えて終了。
2. `specs/<feature>/spec.md` が既に承認済みなら「`/plan <feature>` に進めます」と伝えて終了。

## 手順

1. `spec-writer` サブエージェント（model: fable）に委譲する。渡すもの:
   - `specs/<feature>/intent.md` のパス
   - テンプレート `specs/_template/spec.md` のパス
   - `CLAUDE.md`（規約・Commands セクション）と `.claude/rules/` を読むよう指示
   - 既存 spec.md があれば（未承認の続き）そのパス
2. spec-writer は次を書く:
   - 概要（intent の要約と、この spec が満たす範囲）
   - 要件（機能要件 / 非機能要件）
   - 設計（技術スタック・ディレクトリ構成・データフロー・外部依存）
     - **技術スタックが未定の場合**（CLAUDE.md の Commands が placeholder のとき）は候補を2〜3個比較し推奨を提示、ユーザーの確認を取ってから確定する。確定後は CLAUDE.md の Commands セクション更新が必要なことを報告に含める。
   - 受入基準（下記の厳密な形式）
3. 受入基準は Stop hook（`.claude/hooks/verify-spec.sh`）が機械的にパースする。**必ずこの形式**:

   ```
   - [ ] AC-1: <人間が読める基準>
     verify: <exit 0 で合格となるシェルコマンド>
   ```

   - `verify:` は2スペースインデント、コマンドは1行、CLAUDE.md の Commands セクションのコマンドを使う。
   - 自動検証できない基準（見た目など）は `verify: true` にしたうえで、人間が Deploy 後に確認する旨を基準文に書く。
4. 書き上がったら **要点と受入基準の一覧** をユーザーに提示し、intent との整合（intent に書いてないことを勝手に足していないか）を確認してもらう。
5. 承認の案内:

   ```
   承認するなら spec.md 末尾の承認欄を埋めてください:
     approved_by: <あなたの名前>
     approved_at: YYYY-MM-DD
   ```

6. 修正要望があれば spec-writer に差し戻す。承認されるまで `/plan` に進まない。

## 禁止事項

- intent.md に無い機能を追加しない（必要なら intent の修正を提案する）。
- 受入基準の形式を崩さない。
- コードを書かない。
