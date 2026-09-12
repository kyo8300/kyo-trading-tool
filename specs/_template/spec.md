# Spec: <feature-name>

<!-- 承認済み intent.md を入力に /spec で spec-writer が書く。intent に無い機能は足さない。 -->

元 intent: `specs/<feature-name>/intent.md`（approved_at: YYYY-MM-DD）

## 概要

<!-- intent の要約と、この spec が満たす範囲・満たさない範囲 -->

## 要件

### 機能要件

- FR-1:
- FR-2:

### 非機能要件

- NFR-1: <!-- 性能・セキュリティ・可用性・運用 -->

## 設計

### 技術スタック

<!-- 未定なら候補を比較して推奨を書き、人間の確認後に確定。確定したら CLAUDE.md の Commands を更新する。 -->

### 構成

<!-- ディレクトリ / モジュール構成。新規と既存を区別する。 -->

### データフロー

<!-- 入力 → 処理 → 出力。外部 API・永続化があれば明記。 -->

### 外部依存

<!-- ライブラリ / サービス / 認証情報（値は書かない） -->

## 受入基準

<!--
Stop hook がパースする。形式厳守:
- [ ] AC-n: <人間が読める基準>
  verify: <exit 0 で合格となる1行コマンド>
自動検証できないものは verify: true にし、基準文に「Deploy 後に人間が確認」と書く。
-->

- [ ] AC-1:
  verify:
- [ ] AC-2:
  verify:

## 承認

approved_by:
approved_at:
