#!/bin/sh
# 门锁 SD 录像补录 —— HA 侧入口
#   run_ezviz_pull.sh [--bg] [脚本参数...]
#   --bg：后台运行后立即返回（给 shell_command / 自动化用，避开 shell_command 60 秒上限）
#   不带 --bg：前台运行，日志直接打在终端（SSH 调试用）
set -u

BG=0
if [ "${1:-}" = "--bg" ]; then BG=1; shift; fi

# 脚本与本包装器同目录。HACS 安装后该目录固定为
# /config/custom_components/ezviz_lock/tools/ ，不需要再手工复制到 /config/scripts/。
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# v1.3.1 起默认「全量双路」：停用 SD 单路产出。
# 所有调用（开门 / 徘徊 / 30 分钟兜底）改道到云存储双路补录，
# 由云脚本的进程锁 + 300s 节流负责去重；不改 YAML、不重启 HA。
# 一键回滚：把 SD_PULL_ENABLED 改成 1 即恢复原来的单路 SD 链路。
SD_PULL_ENABLED=0
if [ "$SD_PULL_ENABLED" != "1" ]; then
  case " $* " in
    *" --check "*) exec /bin/sh "$SCRIPT_DIR/run_ezviz_cloud_pull.sh" "$@" ;;
    *) if [ "$BG" = "1" ]; then
         exec /bin/sh "$SCRIPT_DIR/run_ezviz_cloud_pull.sh" --bg "$@"
       else
         exec /bin/sh "$SCRIPT_DIR/run_ezviz_cloud_pull.sh" "$@"
       fi ;;
  esac
fi

SCRIPT="$SCRIPT_DIR/ezviz_sd_pull.py"
LOG_DIR=/config/logs
LOG="$LOG_DIR/ezviz_sd_pull.log"
mkdir -p "$LOG_DIR" 2>/dev/null || true

PY=""
for c in /usr/local/bin/python3 /usr/bin/python3; do
  [ -x "$c" ] && PY="$c" && break
done
[ -z "$PY" ] && PY="$(command -v python3 2>/dev/null || true)"
if [ -z "$PY" ]; then
  echo "[$(date '+%F %T')] 找不到 python3，任务未启动" >>"$LOG"
  exit 4
fi

if [ "$BG" = "1" ]; then
  # setsid 让任务脱离当前会话，HA 重启/超时都不会打断正在跑的取流与转码
  if command -v setsid >/dev/null 2>&1; then
    setsid "$PY" "$SCRIPT" --log-file "$LOG" "$@" </dev/null >/dev/null 2>&1 &
  else
    "$PY" "$SCRIPT" --log-file "$LOG" "$@" </dev/null >/dev/null 2>&1 &
  fi
  echo "[$(date '+%F %T')] 已后台启动补录任务 pid=$! 参数：$*" >>"$LOG"
  exit 0
fi

exec "$PY" "$SCRIPT" "$@"
