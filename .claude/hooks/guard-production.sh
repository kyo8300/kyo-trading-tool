#!/bin/bash
# PreToolUse(Bash) hook: production への deploy は人間の明示的な承認 (RELEASE_APPROVAL) が無いとブロックする。
#
# 入力: stdin に Claude Code の PreToolUse hook JSON ({tool_name, tool_input: {command}, ...})
# 終了コード: 0 = 許可 / 2 = ブロック（stderr の内容が Claude に返る）
set -euo pipefail

HOOK_INPUT=$(cat)
CMD=$(printf '%s' "$HOOK_INPUT" | jq -r '.tool_input.command // ""')

[[ -z "$CMD" ]] && exit 0

# 検査対象はコマンド行のみ。heredoc 本文（コミットメッセージ等）と git commit 行は除外する
# （メッセージ中の "deploy" "production" で誤検知しないため）。
cmd_only=$(printf '%s\n' "$CMD" | awk '
  BEGIN { hd = "" }
  hd != "" { if ($0 == hd) hd = ""; next }
  match($0, /<<-?[ \t]*["'"'"']?[A-Za-z_][A-Za-z0-9_]*["'"'"']?/) {
    tag = substr($0, RSTART, RLENGTH); sub(/^<<-?[ \t]*/, "", tag); gsub(/["'"'"']/, "", tag); hd = tag
  }
  /^[ \t]*git[ \t]+commit/ { next }
  { print }
')
cmd_lower=$(printf '%s' "$cmd_only" | tr '[:upper:]' '[:lower:]')

is_deploy=0
[[ "$cmd_lower" == *deploy* ]] && is_deploy=1

is_production=0
if [[ "$cmd_lower" == *production* || "$cmd_lower" =~ (^|[^a-z])prod([^a-z]|$) ]]; then
  is_production=1
fi

if [[ $is_deploy -eq 1 && $is_production -eq 1 && -z "${RELEASE_APPROVAL:-}" ]]; then
  {
    echo "🛑 guard-production: production への deploy には人間のリリース承認が必要です。"
    echo "   ブロックしたコマンド: ${CMD}"
    echo "   人間が RELEASE_APPROVAL=<name> を設定してセッションを再開するか、このコマンドを自分で実行してください。"
  } >&2
  exit 2
fi

exit 0
