# Review instructions

reviewer エージェントがコードレビュー時に読む方針。人間がレビューする場合も同じ基準を使う。

## Passes

3つのパスを順番に、別々に実施する。各 finding にどのパスで見つけたかをタグ付けする。

- **Bugs**: ロジック誤り、境界条件の欠落、回帰、未処理の例外・エラー、競合状態、リソースリーク
- **Security**: インジェクション（SQL / コマンド / パス）、認証・認可の抜け、秘密情報のハードコードや露出、ログへの PII、信頼できない外部入力の未検証
- **Compliance**: 変更が `specs/<feature>/spec.md` の要件・設計と一致しているか。`specs/<feature>/plan.md` の変更ファイル・作業順序から逸脱していないか。`CLAUDE.md` と `.claude/rules/` の規約（イミュータブル、`any` 禁止、ファイル 800 行以下、関数 50 行以下、境界での入力検証、エラーの明示的処理）に従っているか

## What Important means here

**Important** は以下のいずれかに該当する finding のみ:

- 動作を壊す（誤った結果、クラッシュ、データ破損）
- データを漏らす（秘密情報、PII、権限外のアクセス）
- 方針に違反する（spec.md の要件を満たさない、plan.md にない設計変更、`.claude/rules/` の CRITICAL 規約違反）

スタイル、命名、コメントの有無、些細な重複は **nit**。

Important が 1 件以上 → REJECT。0 件 → APPROVE。plan.md の前提そのものが崩れている → HALT。

## Cap the nits

nit は最大 5 件まで報告する。超過分は件数のみ要約する（「他 n 件の nit」）。nit だけで REJECT しない。

## Do not report

- linter / 型チェッカー / コンパイラ / CI が検出するもの（import 漏れ、型エラー、フォーマット）
- 変更範囲外の既存の問題（ただし変更がそれを悪化させる場合は Important）
- テストカバレッジ不足・ドキュメント不足などの一般論（spec.md や `.claude/rules/` が明示的に要求している場合を除く）
- 自動生成ファイル、ロックファイル
- コード内で明示的に抑制されているもの（lint ignore コメント付き）
- 確信のない推測。確認できない場合は「要確認」として nit に置く

## Repeat reviews

REJECT 後の再レビューでは、まず前回の Important が解消されているかを確認する。解消されていれば同じ指摘を繰り返さない。新規の Important のみ追加する。
