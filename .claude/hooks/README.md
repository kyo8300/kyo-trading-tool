# Hooks

`.claude/settings.json` から呼ばれる。いずれも stdin に JSON を受け取り、
exit 0 で許可、exit 2 でブロック（stderr の内容が Claude に返る）。

## verify-spec.sh（Stop）

ハーネス実行中（`.claude/harness.local.json` の `status == "running"`）に、Claude が終了しようとするたびに
`specs/<feature>/spec.md` の `## 受入基準` の `verify:` コマンドを全部実行する。

| 状況 | 動作 |
|---|---|
| 状態ファイルなし / status が running でない | exit 0（何もしない） |
| session_id が状態ファイルと不一致 | exit 0（別セッションのループには干渉しない） |
| iteration >= max_iterations | status=halted に更新して exit 0（人間へ） |
| verify 全部 exit 0 | status=done に更新して exit 0 |
| 1つでも失敗 | iteration+1 を保存し、失敗 AC と出力末尾を stderr に出して **exit 2** |

受入基準の形式（これ以外はパースされない）:

```
## 受入基準

- [ ] AC-1: 単体テストが全部通る
  verify: npm test
- [ ] AC-2: 型エラーがない
  verify: npm run typecheck
```

環境変数 `HARNESS_VERIFY_TIMEOUT_SEC`（既定 300）で各 verify のタイムアウトを変えられる。

### 手動テスト

```sh
# 1. 状態ファイルと spec を用意
cat > .claude/harness.local.json <<'EOF'
{"feature":"demo","status":"running","iteration":0,"max_iterations":8,"review_rejections":0,"max_review_rejections":5,"session_id":"test"}
EOF
mkdir -p specs/demo && cat > specs/demo/spec.md <<'EOF'
## 受入基準
- [ ] AC-1: 通る
  verify: true
- [ ] AC-2: 落ちる
  verify: false
EOF

# 2. 実行（exit 2 と失敗一覧が出るはず）
echo '{"session_id":"test","stop_hook_active":false}' | bash .claude/hooks/verify-spec.sh; echo "exit=$?"

# 3. AC-2 の verify を true にすると exit 0 / status=done になる
# 4. 後片付け
rm -rf specs/demo .claude/harness.local.json
```

## guard-production.sh（PreToolUse: Bash）

コマンドに `deploy` と `production`（または単語 `prod`）の両方を含み、環境変数 `RELEASE_APPROVAL` が空なら exit 2。

```sh
echo '{"tool_input":{"command":"vercel deploy --prod"}}' | bash .claude/hooks/guard-production.sh; echo "exit=$?"   # 2
RELEASE_APPROVAL=kyo bash -c 'echo "{\"tool_input\":{\"command\":\"vercel deploy --prod\"}}" | bash .claude/hooks/guard-production.sh'; echo "exit=$?"  # 0
echo '{"tool_input":{"command":"npm test"}}' | bash .claude/hooks/guard-production.sh; echo "exit=$?"  # 0
```

## 依存

- `jq`（必須）
- `timeout` / `gtimeout` があれば使う。無ければ perl の alarm で代替
