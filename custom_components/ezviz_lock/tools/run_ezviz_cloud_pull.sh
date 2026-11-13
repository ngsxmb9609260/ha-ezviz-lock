#!/bin/sh
# 门锁「云存储录像」补录（recType=1 / type=3）—— HA 侧入口
#   run_ezviz_cloud_pull.sh [--bg] [脚本参数...]
#   与本包装器同目录的 ezviz_cloud_pull.py；HACS 安装后目录固定为
#   /config/custom_components/ezviz_lock/tools/ ，不用手工复制到 /config/scripts/。
set -u

BG=0
if [ "${1:-}" = "--bg" ]; then BG=1; shift; fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SCRIPT="$SCRIPT_DIR/ezviz_cloud_pull.py"
LOG_DIR=/config/logs
LOG="$LOG_DIR/ezviz_cloud_pull.log"
mkdir -p "$LOG_DIR" 2>/dev/null || true

PY=""
for c in /usr/local/bin/python3 /usr/bin/python3; do
  [ -x "$c" ] && PY="$c" && break
done
[ -z "$PY" ] && PY="$(command -v python3 2>/dev/null || true)"
if [ -z "$PY" ]; then
  echo "[$(date '+%F %T')] 找不到 python3，云补录任务未启动" >>"$LOG"
  exit 4
fi

if [ "$BG" = "1" ]; then
  if command -v setsid >/dev/null 2>&1; then
    setsid "$PY" "$SCRIPT" --log-file "$LOG" "$@" </dev/null >/dev/null 2>&1 &
  else
    "$PY" "$SCRIPT" --log-file "$LOG" "$@" </dev/null >/dev/null 2>&1 &
  fi
  echo "[$(date '+%F %T')] 已后台启动云补录任务 pid=$! 参数：$*" >>"$LOG"
  exit 0
fi

exec "$PY" "$SCRIPT" "$@"
