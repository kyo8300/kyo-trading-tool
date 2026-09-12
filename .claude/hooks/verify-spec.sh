#!/bin/bash
# Stop hook: ハーネス実行中は spec.md の受入基準 (verify:) が全部通るまで終了をブロックする。
#
# 入力: stdin に Claude Code の Stop hook JSON ({session_id, stop_hook_active, ...})
# 状態: .claude/harness.local.json
# 終了コード: 0 = 終了を許可 / 2 = 終了をブロック（stderr の内容が Claude に返る）
set -euo pipefail

STATE_FILE=".claude/harness.local.json"
VERIFY_TIMEOUT_SEC="${HARNESS_VERIFY_TIMEOUT_SEC:-300}"
OUTPUT_TAIL_LINES=20

# --- 状態ファイルが無い / running でない → 通常終了 ---
[[ -f "$STATE_FILE" ]] || exit 0

HOOK_INPUT=$(cat)
HOOK_SESSION=$(printf '%s' "$HOOK_INPUT" | jq -r '.session_id // ""')

STATUS=$(jq -r '.status // ""' "$STATE_FILE")
[[ "$STATUS" == "running" ]] || exit 0

STATE_SESSION=$(jq -r '.session_id // ""' "$STATE_FILE")
if [[ -n "$STATE_SESSION" && -n "$HOOK_SESSION" && "$STATE_SESSION" != "$HOOK_SESSION" ]]; then
  # 別セッションが回しているループ。このセッションは干渉しない
  exit 0
fi

FEATURE=$(jq -r '.feature // ""' "$STATE_FILE")
ITERATION=$(jq -r '.iteration // 0' "$STATE_FILE")
MAX_ITERATIONS=$(jq -r '.max_iterations // 8' "$STATE_FILE")

# 状態ファイルを原子的に更新する: update_state '<jq filter>'
update_state() {
  local tmp
  tmp=$(mktemp "${STATE_FILE}.XXXXXX")
  jq "$1" "$STATE_FILE" > "$tmp" && mv "$tmp" "$STATE_FILE"
}

if [[ -z "$FEATURE" ]]; then
  echo "verify-spec: 状態ファイルに feature がありません。ループを停止します。" >&2
  update_state '.status = "halted"'
  exit 0
fi

SPEC_FILE="specs/${FEATURE}/spec.md"
if [[ ! -f "$SPEC_FILE" ]]; then
  echo "verify-spec: ${SPEC_FILE} が見つかりません。ループを停止します。" >&2
  update_state '.status = "halted"'
  exit 0
fi

if ! [[ "$ITERATION" =~ ^[0-9]+$ && "$MAX_ITERATIONS" =~ ^[0-9]+$ ]]; then
  echo "verify-spec: 状態ファイルの iteration/max_iterations が数値ではありません。ループを停止します。" >&2
  update_state '.status = "halted"'
  exit 0
fi

# --- 上限チェック: 人間に返す ---
if [[ "$ITERATION" -ge "$MAX_ITERATIONS" ]]; then
  update_state '.status = "halted"'
  {
    echo "🛑 verify-spec: Stop hook の上限 (${MAX_ITERATIONS} 回) に達しました。status=halted。"
    echo "   feature: ${FEATURE} — 人間が ${SPEC_FILE} と plan.md を見て、続行/中止を判断してください。"
  } >&2
  exit 0
fi

# --- 受入基準のパース ---
# 形式:
#   - [ ] AC-1: <基準>
#     verify: <command>
# 「## 受入基準」セクション内のみ対象。次の "## " 見出しで終了。
parse_acceptance_criteria() {
  awk '
    /^## /            { in_section = ($0 ~ /^## 受入基準/) ; next }
    !in_section       { next }
    /^- \[[ xX]\] AC-/ {
      id = $0
      sub(/^- \[[ xX]\] /, "", id)
      sub(/:.*$/, "", id)
      next
    }
    /^[[:space:]]+verify:[[:space:]]*/ && id != "" {
      cmd = $0
      sub(/^[[:space:]]+verify:[[:space:]]*/, "", cmd)
      if (cmd != "") print id "\t" cmd
      id = ""
    }
  ' "$SPEC_FILE"
}

CRITERIA=$(parse_acceptance_criteria)
if [[ -z "$CRITERIA" ]]; then
  echo "verify-spec: ${SPEC_FILE} の '## 受入基準' に verify: 付きの AC が見つかりません。ループを停止します。" >&2
  update_state '.status = "halted"'
  exit 0
fi

# --- verify 実行 ---
# timeout コマンドが無い macOS では perl でラップする
run_with_timeout() {
  if command -v timeout >/dev/null 2>&1; then
    timeout "$VERIFY_TIMEOUT_SEC" bash -c "$1"
  elif command -v gtimeout >/dev/null 2>&1; then
    gtimeout "$VERIFY_TIMEOUT_SEC" bash -c "$1"
  else
    perl -e 'alarm shift; exec @ARGV' "$VERIFY_TIMEOUT_SEC" bash -c "$1"
  fi
}

PASSED=()
FAILED=()
FAILED_DETAIL=""
TOTAL=0

while IFS=$'\t' read -r ac_id ac_cmd; do
  [[ -z "$ac_id" ]] && continue
  TOTAL=$((TOTAL + 1))
  set +e
  output=$(run_with_timeout "$ac_cmd" 2>&1)
  rc=$?
  set -e
  if [[ $rc -eq 0 ]]; then
    PASSED+=("$ac_id")
  else
    FAILED+=("$ac_id")
    tail_out=$(printf '%s\n' "$output" | tail -n "$OUTPUT_TAIL_LINES")
    FAILED_DETAIL+="
--- ${ac_id} (exit ${rc}) ---
\$ ${ac_cmd}
${tail_out}
"
  fi
done <<< "$CRITERIA"

# --- 判定 ---
if [[ ${#FAILED[@]} -eq 0 ]]; then
  update_state '.status = "done"'
  echo "✅ verify-spec: 受入基準 ${TOTAL}/${TOTAL} 合格。status=done。" >&2
  exit 0
fi

NEXT_ITERATION=$((ITERATION + 1))
update_state ".iteration = ${NEXT_ITERATION}"

{
  echo "❌ verify-spec: 受入基準 未達 ${#FAILED[@]}/${TOTAL} (iteration ${NEXT_ITERATION}/${MAX_ITERATIONS})"
  echo "   feature: ${FEATURE}  spec: ${SPEC_FILE}"
  echo "   合格: ${PASSED[*]:-なし}"
  echo "   失敗: ${FAILED[*]}"
  echo "$FAILED_DETAIL"
  echo "失敗した AC を満たすまで作業を続けてください。テストを書き換えて通すのは禁止です（実装を直す）。"
  echo "残り ${MAX_ITERATIONS} 回中 $((MAX_ITERATIONS - NEXT_ITERATION)) 回で上限に達し、人間に返されます。"
} >&2
exit 2
