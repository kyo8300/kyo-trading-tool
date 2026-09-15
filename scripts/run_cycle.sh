#!/usr/bin/env bash
# cron から呼ぶ run-cycle ラッパー。
# - リポジトリ直下に移動し、.env を読み込み、`trader run-cycle` を 1 回実行する
# - 出力は var/run-cycle.log に追記（DB とロックは var/ 配下、git 管理外）
# - 二重起動は trader 側の var/run-cycle.lock で防がれる（N-10）
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if [[ ! -f .env ]]; then
  echo "run_cycle.sh: .env がありません（.env.example を元に作成してください）" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

mkdir -p var
UV_BIN="${UV_BIN:-$(command -v uv || echo "$HOME/.local/bin/uv")}"
{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) run-cycle start"
  "$UV_BIN" run trader run-cycle
  echo "=== exit $?"
} >> var/run-cycle.log 2>&1
