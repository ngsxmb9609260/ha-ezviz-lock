#!/bin/sh
# 门锁录像补录 —— HA 侧入口（v1.4.0：SD 卡优先，云端兜底）
#   run_ezviz_pull.sh [--bg] [脚本参数...]
#   --bg：后台运行后立即返回（给 shell_command / 自动化用，避开 shell_command 60 秒上限）
#   不带 --bg：前台运行，日志直接打在终端（SSH 调试用）
#
# 执行顺序
#   1) 先跑 ezviz_sd_pull.py：直接从锁的 SD 卡回放取（画质最高、不吃云存储配额，
#      带 AAC 音轨 + 上下双路拼合）
#   2) SD 没产出（通道不可用 / 没有可补的段 / 部分失败）时，自动改走
#      run_ezviz_cloud_pull.sh：云存储回放，同样双路 + 声音
#   两条链路写同一个目录、同一份 clean_clips.json，并且互相按时间窗口去重，
#   同一时刻不会出现 SD 与云两份重复片。
#
# 一键切换（改下面两个开关，不动 YAML、不重启 HA）：
#   SD_FIRST=0        全量走云端（等价 v1.3.1 的行为）
#   CLOUD_FALLBACK=0  只走 SD，不兜底
#   SD_DUAL=0         SD 侧不出双路（只出 A 路，便于排查）
set -u

BG=0
if [ "${1:-}" = "--bg" ]; then BG=1; shift; fi

# 脚本与本包装器同目录。HAOS 部署在 /config/scripts/，HACS 安装在
# /config/custom_components/ezviz_lock/tools/ ，两处都能直接用。
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ ! -f "$SCRIPT_DIR/ezviz_sd_pull.py" ] && [ -f /config/scripts/ezviz_sd_pull.py ]; then
  SCRIPT_DIR=/config/scripts
fi
SELF="$SCRIPT_DIR/$(basename -- "$0")"
SD_SCRIPT="$SCRIPT_DIR/ezviz_sd_pull.py"
CLOUD_WRAPPER="$SCRIPT_DIR/run_ezviz_cloud_pull.sh"

SD_FIRST=1
CLOUD_FALLBACK=1
SD_DUAL=1
SD_DUAL_WIDTH=720

LOG_DIR=/config/logs
SD_LOG="$LOG_DIR/ezviz_sd_pull.log"
CLOUD_LOG="$LOG_DIR/ezviz_cloud_pull.log"
mkdir -p "$LOG_DIR" 2>/dev/null || true

PY=""
for c in /usr/local/bin/python3 /usr/bin/python3; do
  [ -x "$c" ] && PY="$c" && break
done
[ -z "$PY" ] && PY="$(command -v python3 2>/dev/null || true)"
if [ -z "$PY" ]; then
  echo "[$(date '+%F %T')] 找不到 python3，任务未启动" >>"$SD_LOG"
  exit 4
fi

# --- 环境自检：两条链路都查（各自打印结果，任一失败即非 0） ----------
case " $* " in
  *" --check "*)
    rc1=0
    if [ "$SD_FIRST" = "1" ]; then
      "$PY" "$SD_SCRIPT" "$@"
      rc1=$?
    fi
    /bin/sh "$CLOUD_WRAPPER" "$@"
    rc2=$?
    [ "$rc1" -eq 0 ] && [ "$rc2" -eq 0 ] && exit 0
    exit 4
    ;;
esac

# --- 后台模式：把自己重新拉起来跑链式流程 ---------------------------
if [ "$BG" = "1" ]; then
  # setsid 让任务脱离当前会话，HA 重启/超时都不会打断正在跑的取流与转码
  if command -v setsid >/dev/null 2>&1; then
    setsid /bin/sh "$SELF" --chain "$@" </dev/null >/dev/null 2>&1 &
  else
    /bin/sh "$SELF" --chain "$@" </dev/null >/dev/null 2>&1 &
  fi
  echo "[$(date '+%F %T')] 已后台启动补录任务 pid=$! 参数：$*" >>"$SD_LOG"
  exit 0
fi

[ "${1:-}" = "--chain" ] && shift

# --- 链式主体（前台执行；--bg 时由上面的分支拉起） -------------------
if [ "$SD_FIRST" != "1" ]; then
  exec /bin/sh "$CLOUD_WRAPPER" "$@"
fi

set -- --log-file "$SD_LOG" "$@"
[ "$SD_DUAL" = "1" ] && set -- --dual-road --dual-width "$SD_DUAL_WIDTH" "$@"

"$PY" "$SD_SCRIPT" "$@"
rc=$?

case "$rc" in
  0)
    exit 0 ;;
  1|2|5|6)
    # 1=部分失败 2=回放通道不可用 5=SD 侧节流跳过 6=没有可补的段
    if [ "$CLOUD_FALLBACK" = "1" ]; then
      echo "[$(date '+%F %T')] SD 未产出（rc=$rc），改走云端兜底" >>"$SD_LOG"
      # 末尾的 --log-file 覆盖前面注入的 SD 日志路径，让云脚本写自己的日志
      exec /bin/sh "$CLOUD_WRAPPER" "$@" --log-file "$CLOUD_LOG"
    fi
    echo "[$(date '+%F %T')] SD 未产出（rc=$rc），CLOUD_FALLBACK=0，不兜底" >>"$SD_LOG"
    exit "$rc" ;;
  *)
    # 3=另一个进程正在跑 4=环境不可用：都不该再叠加一次云端任务
    echo "[$(date '+%F %T')] SD 跳过（rc=$rc），不回退云端" >>"$SD_LOG"
    exit "$rc" ;;
esac
