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

# cron の PATH は /usr/bin:/bin だけなので、uv を既知の場所から探す。
# crontab で UV_BIN=/path/to/uv を指定すればそれが優先される。
find_uv() {
  if [[ -n "${UV_BIN:-}" ]]; then
    echo "$UV_BIN"
    return
  fi
  local candidate
  for candidate in \
    "$(command -v uv 2>/dev/null || true)" \
    "$HOME/.local/bin/uv" \
    "$HOME/.cargo/bin/uv" \
    /opt/homebrew/bin/uv \
    /usr/local/bin/uv; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      echo "$candidate"
      return
    fi
  done
  echo "run_cycle.sh: uv が見つかりません（crontab に UV_BIN=/path/to/uv を追加してください）" >&2
  exit 1
}
UV_BIN="$(find_uv)"
{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) run-cycle start"
  "$UV_BIN" run trader run-cycle
  echo "=== exit $?"
} >> var/run-cycle.log 2>&1
