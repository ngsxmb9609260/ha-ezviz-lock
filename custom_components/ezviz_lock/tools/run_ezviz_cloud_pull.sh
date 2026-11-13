#!/bin/sh
# 门锁「云存储录像」补录（recType=1 / type=3）—— HA 侧入口
#   run_ezviz_cloud_pull.sh [--bg] [脚本参数...]
#   --bg：后台运行后立即返回（给 shell_command / 自动化用，避开 60 秒上限）
#   与 SD 链路完全独立：不同脚本、不同日志、不同锁文件、不同节流状态
set -u

BG=0
if [ "${1:-}" = "--bg" ]; then BG=1; shift; fi

# 2026-11-13 17:28 主人确认启用：上摄在上 / 下摄在下的双路拼合。
# 一键回退：把 DUAL_ROAD 改成 0 即可恢复单路 A，不用动 configuration.yaml，
# 也不用重启 HA（shell_command 的命令字符串没变，下次触发即生效）。
DUAL_ROAD=1
case " $* " in
  *" --check "*) ;;                 # --check 自检不注入
  *) [ "$DUAL_ROAD" = "1" ] && set -- "$@" --dual-road || true ;;
esac

SCRIPT=/config/scripts/ezviz_cloud_pull.py
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
