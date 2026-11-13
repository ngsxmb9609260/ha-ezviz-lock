# EZVIZ Lock · 萤石智能门锁 Home Assistant 集成

> 通过 HACS 安装。把萤石智能门锁的**开门记录、抓拍、徘徊告警**接进 Home Assistant，
> 并把**门锁本地 SD 卡录像**拉回 HAOS、清洗成浏览器可直接播放的 H.264 mp4。

本仓库由一套已在真机长期运行的集成原样打包而成，**不含任何 AppKey / AppSecret / 令牌 / 内网地址**。
凭据在配置流程里填写，保存在 HA 自己的 `.storage` 中。

---

## 目录

1. [它能做什么](#1-它能做什么)
2. [已实测 / 未验证](#2-已实测--未验证)
3. [前置条件](#3-前置条件)
4. [安装（HACS）](#4-安装hacs)
5. [前端面板](#5-前端面板)
6. [SD 卡录像补录链路](#6-sd-卡录像补录链路)
7. [服务一览](#7-服务一览)
8. [排障](#8-排障)
9. [升级 / 卸载 / 回滚](#9-升级--卸载--回滚)
10. [已知限制](#10-已知限制)

---

## 1. 它能做什么

两条彼此独立的能力，装了集成就有第一条，第二条需要额外做一次性配置（第 6 节）：

| 能力 | 内容 |
|---|---|
| **门锁接入** | 20 秒轮询萤石开放平台 → 开门记录（人员 / 开锁方式 / 人数 / 时间）、徘徊告警、抓拍图；输出 `sensor` / `event` / `image` 实体 |
| **视频拉取** | 查询门锁本地 SD 卡录像段 → 让设备把该段重放成 FLV 流 → 拆流清洗 → H.264 mp4 → 干净白名单 → 自动匹配到开门 / 徘徊记录 |

```
萤石开放平台 open.ys7.com
   ├─ /api/lapp/alarm/list                    告警（开门 / 徘徊）
   ├─ /api/lapp/video/by/time  recType=2     SD 卡录像段清单
   └─ /api/lapp/v2/live/address/get type=2  把某段 SD 录像推成 FLV 流
                    │
                    ▼
        ffmpeg -c copy  →  原始 HEVC 流
                    │
        Annex-B NAL 奇偶拆成 A / B 两路 → 逐帧绿占比统计 → 只留干净那一路
                    │
        libx264 15fps H.264 mp4 + 逐帧绿自检
                    │
        /media/tpcamera/door-lock/<日期>/<HHMMSS>_sd.mp4  +  clean_clips.json 白名单
                    │
        集成按开门时刻 ±30s / 徘徊 ±45s 认领 → 前端 <video> 播放
```

### 为什么必须「拆流去绿」

这不是 ffmpeg 参数或解码器的问题，也不是换个协议就能绕开：
回放流把**一路正常 HEVC** 与**一路不可解码 NAL** 严格交替塞进同一条流，
解码器把后者当成 `Duplicate POC` 丢包，未解码宏块输出为大片纯绿。

实测（同一段录像）：直接解码 60 帧里 59 帧绿、均值 87.0%；
按帧 NAL 奇偶拆开后 **A 路 76 帧 0% 绿 / B 路 76 帧 93.6% 绿**；
A 路重编码成品逐帧 0.00% 绿。软解 / 硬解 / `+genpts` / `ignore_err` / 关键帧重解全部无效。

`tools/ezviz_sd_pull.py` 与 `hevc_split.py` 就是这段清洗逻辑的实现。
`clean_clips.json` 只接受 `green_ratio <= 0.02` 且 `frames >= 15` 的成片，
集成还会复核文件 size + md5，前端再过滤一次——**绿片没有机会冒充开门录像**。

---

## 2. 已实测 / 未验证

> 先把边界说清，避免把「打包成功」当成「全新环境装过」。

**已实测**（原系统，HAOS `2026.10.4`，单台 Y3000FVX Plus）：

| 项目 | 结果 |
|---|---|
| 打包源码 | 从 HAOS `/config` 经 SSH + tar 拉回 19 个文件，逐个 md5 与远端一致 |
| 视频清洗 | 连续 14 段补录全部逐帧 **0.00% 绿**；成片 `h264 High 1504x1968`，浏览器可播 |
| 自动认领 | 集成按 SD 段起点与开门时刻偏差 1/2/2/3/5/12 秒正确挂载；±30s 窗口修掉了「晚 58 秒的错片」 |
| 调度 | `shell_command` 调用 0.01 秒返回，后台 78 秒拉完一段；30 分钟兜底自然触发、事件触发节流拦截均实测 |
| 冷却 | 短时间高频取流会触发回放 URL 404，脚本整批中止并降权重试，**不猛打设备** |
| 依赖 | 运行链路零依赖 Samba；SSH 只作维护通道 |

**未验证 / 必须知道**：

1. **本仓库没有在一台全新的 HA 上从零装过一遍。** 为避免打断现网，打包后只做了静态验证（语法、JSON/YAML、合规项、敏感信息扫描），没有真的用 HACS 装一次再跑一轮录像。
2. 其它 HA 版本、其它萤石门锁型号、**多台门锁并发**、长时间 `--watch` 稳定性，均未验证。
3. 浏览器端 `<video>` 首帧渲染、iframe 面板高度只在本机浏览器看过，未做跨浏览器验证。
4. 「开门瞬间」实时抓录链路（另起实验页那条）**不在本集成内**；本集成的录像都是**事后从 SD 卡回放拉取**，按时间就近匹配，不保证正好拍到开门那一下。
5. B 路 NAL 到底是什么（双摄另一路 / 加密数据），在现有 API 权限下无法确认，脚本直接丢弃它；A 路帧率约为标称 30fps 的一半。

---

## 3. 前置条件

| 项 | 要求 |
|---|---|
| Home Assistant | **2024.11+**（开发 / 实测环境 HAOS `2026.10.4`） |
| HACS | 2.0+ |
| 萤石开放平台 | AppKey / AppSecret。**AppKey 需开通 `keylock/open/list` 开门记录接口白名单**，否则只有告警与抓拍图，拿不到「人员 / 开锁方式」；配置流程会探测并提示 |
| ffmpeg | 需具备 `libx264`、HEVC 解码、`hevc_mp4toannexb`。HAOS 主容器自带 8.1.2 已满足；太旧时把单文件静态版放到 `/config/bin/ffmpeg` 即可，不用改脚本、不用重启 HA |
| 存储 | `/media` 可写（HAOS 默认可写） |
| 门锁 | 已插 SD 卡、未开启设备加密（`isEncrypt=1` 时回放取流会报 `60090`） |

> **仓库可见性**：本仓库必须是 **Public**。HACS 官方 FAQ 明确：
> *"Private GitHub repositories can not be used with HACS at all. HACS can only get publicly available information."*
> 私有仓库没有 token 绕过办法。代码里没有任何密钥，公开不会泄露凭据。

---

## 4. 安装（HACS）

### 4.1 添加自定义仓库

1. HACS → **集成** → 右上角 `⋮` → **自定义存储库**
2. 仓库地址填 `https://github.com/ngsxmb9609260/ha-ezviz-lock`，类别选 **Integration**
3. 搜索 `EZVIZ Lock` → **下载** → **重启 Home Assistant**

> 也可以点这个 my-link 直接跳转（把链接里的仓库路径换成实际地址）：
> `https://my.home-assistant.io/redirect/hacs_repository/?owner=ngsxmb9609260&repository=ha-ezviz-lock&category=integration`

> 可选 CI：仓库随附 `examples/validate.yml.example`（HACS 官方校验工作流模板）。
> 需要时复制到 `.github/workflows/validate.yml` 即可启用；GitHub 要求推送 workflow 的
> 凭据具备 `workflow` 权限，所以默认不放入该目录，不影响安装。

### 4.2 配置集成

设置 → 设备与服务 → **添加集成** → 搜索 `EZVIZ Lock`，填写：

| 字段 | 说明 |
|---|---|
| AppKey / AppSecret | 萤石开放平台 → 控制台 → 应用管理 |
| 设备序列号 | 萤石 App → 设备设置 → 设备信息，或开放平台控制台设备列表 |
| 通道号 | 默认 `1` |
| 接口区域 | 默认中国 `https://open.ys7.com`，另有备用 / 欧洲 / 北美节点 |

配置流程会实际调用一次 `device/info` 校验；若开门记录接口没开白名单，会弹二次确认页说明后果，可先完成添加再去找萤石开通。

选项（集成 → 配置）里可改：轮询间隔（默认 20s）、事件聚合窗口、抓拍保留天数、通道号、事件即时抓拍开关、告警类型名称映射、用户别名（`用户1=爸爸` 这种）。

### 4.3 实体

装好后会生成（`<slug>` 由设备名生成）：

- `sensor.<slug>_opens_today` / `<slug>_loiter_alerts_today`——属性里带 `recent_events` / `today_events` / `loiter_events` / `sd_clips`
- `sensor.<slug>_last_open_time` / `last_opened_by` / `last_unlock_method` / `last_pass_through_headcount`
- `event.<slug>_door_open_event`——11 种 `event_type`，供自动化触发
- `image.<slug>_last_snapshot`

---

## 5. 前端面板

> **这一步必须手动做，是 HACS 的机制限制，不是安装失败。**
> HACS 只复制 `custom_components/` 一个目录，仓库根之外的 `www/` 不会被放到 `/config/www/`。
> 因此前端 JS 随包放在 `custom_components/ezviz_lock/www/`，需要复制一次（以后 HACS 升级不会覆盖 `/config/www/` 那份，升级后需重新复制）。

**第 1 步：放好卡片资源**

通过 Advanced SSH & Web Terminal 或文件管理器执行：

```bash
mkdir -p /config/www/ezviz_lock
cp /config/custom_components/ezviz_lock/www/door-lock-view.js /config/www/ezviz_lock/
```

设置 → 仪表盘 → 右上角 `⋮` → **资源** → 添加：

| 字段 | 值 |
|---|---|
| URL | `/local/ezviz_lock/door-lock-view.js` |
| 类型 | **JavaScript 模块** |

**第 2 步：建面板视图**

新建仪表盘视图（或用 YAML 模式），把 [`examples/lovelace_dashboard.yaml`](examples/lovelace_dashboard.yaml) 的内容粘进去。
它含 5 个视图：**总览 / 今日明细 / 抓拍 / 历史记录 / 设备信息**。

> ⚠️ 示例里的 entity_id 是占位符，必须全局替换成自己的：
> `y3000fvx_plus_yourserial` → 你的实体前缀，`your_ezviz_lock` → 你的设备实体名。
> 到「开发者工具 → 状态」搜 `opens_today` 就能查到实际 ID。

改完**浏览器 Ctrl+F5 强刷**。

---

## 6. SD 卡录像补录链路

> 不做也能用集成（开门记录 / 抓拍 / 徘徊都在），只是「视频」列没有录像。

### 6.1 三步配置

**① `configuration.yaml`**：把 [`examples/configuration.yaml`](examples/configuration.yaml) 的 `shell_command:` 块粘到末尾
（已有 `shell_command:` 顶层键就只合并子项，别写第二个）。核心是：

```yaml
shell_command:
  ezviz_sd_pull: >-
    /bin/sh /config/custom_components/ezviz_lock/tools/run_ezviz_pull.sh
    --bg --limit 1 --event-priority
```

`--bg` 让脚本脱离会话后台跑——单段实测要 60~90 秒，而 `shell_command` 有内置 60 秒超时，
前台跑会被 kill 留下半截文件。包装器用 `setsid`，HA 重启也不打断正在跑的转码。

**② `automations.yaml`**：把 [`examples/automation_sd_pull.yaml`](examples/automation_sd_pull.yaml) 粘进去，
把里面的 `event.y3000fvx_plus_yourserial_door_open_event` 和
`sensor.your_ezviz_lock_loiter_alerts_today` 换成自己的实体 ID。
（也可以直接在 UI 里导入这段 YAML。）

**③ 检查配置 → 重启 HA**，然后开发者工具 → 动作 → `shell_command.ezviz_sd_pull_check` 做一次自检。

### 6.2 凭据怎么给脚本（不用写进 YAML）

脚本默认读 `/config/.storage/core.config_entries`——那正是本集成 ConfigFlow 写入 AppKey/AppSecret 的地方，
所以**配置完集成，脚本就能用，YAML 里不出现任何密钥**。

如需覆盖，脚本支持环境变量：`EZVIZ_APP_KEY` / `EZVIZ_APP_SECRET` / `EZVIZ_SERIAL` /
`EZVIZ_REGION_HOST` / `EZVIZ_CONFIG_ENTRIES` / `EZVIZ_DEST` / `EZVIZ_STATE_GLOB` /
`FFMPEG_BIN` / `EZVIZ_WORK_DIR`。优先用 `core.config_entries`，避免密钥落到 YAML 里。

### 6.3 为什么要低频

回放通道对短时高频取流很敏感：一次性连拉 13 段会全部拿到 0 字节，
FLV 回放 URL 变 HTTP 404，冷却约 15~20 分钟（确切成因未验证）。
一晚约 12~14 段是实测上限。因此链路自带**四道闸**：

| 闸 | 值 | 作用 |
|---|---|---|
| 定时兜底 | 每 30 分钟 1 段 | 无人开门时也慢慢补齐历史 |
| 事件触发延迟 | 180 秒 | 等设备把这段写进 SD 并同步回放索引 |
| 脚本最小间隔 | 600 秒 | 挡住密集开门打爆通道（定时轮与事件轮共用） |
| 进程锁 | `fcntl.flock` 非阻塞 | 防并发叠加取流 |

> 600 秒必须**小于** 30 分钟定时周期，否则定时轮会被自己上一轮挡住。

### 6.4 验收

```bash
# 环境自检（凭据 / ffmpeg 能力 / 目标可写），rc=4 会打印人话原因
sudo python3 /config/custom_components/ezviz_lock/tools/ezviz_sd_pull.py --check

# 只列清单不取流
sudo python3 /config/custom_components/ezviz_lock/tools/ezviz_sd_pull.py --dry-run --hours 24 --event-priority

tail -f /config/logs/ezviz_sd_pull.log
```

看 `/media/tpcamera/door-lock/clean_clips.json`：只有 `green_ratio <= 0.02`、`frames >= 15`、
`usable_for_open=true` 且 size/md5 与真实文件一致的片才会被前端认领。
想换落盘目录，给 `shell_command` 加 `EZVIZ_DEST=/media/你想要的目录`。

退出码：`0` 全成功 / `1` 有单段失败 / `2` 通道不可用整批中止 /
`3` 已有实例在跑 / `4` 自检不过 / `5` 未到最小间隔被节流。

### 6.5 云存储录像补录（`type=3`，开门瞬间 + 声音）

如果设备开通了萤石**云存储**，可以额外启用一条更精确的补录链路。它和 SD 链路**完全独立**
（不同脚本、不同日志、不同锁/节流状态），共用同一份 `clean_clips.json` 白名单：

| 对比 | SD 回放 (`type=2`) | 云回放 (`type=3`) |
|---|---|---|
| 数据源 | 门锁本地 SD 卡，需设备在线配合 | 云端已存好的事件短片，不打扰设备 |
| 时间点 | 段起点与开门差 1~12 秒，需 ±30 秒窗口校对 | 事件触发即生成，实测多数只差 0~2 秒 |
| 音轨 | **无** | **AAC 16 kHz mono**（成品保留） |
| 通道冷却 | 连续 12~14 段后必 404，需 15~20 分钟冷却 | 实测连续 4 段未见冷却（长期上限未验证） |
| 产出 | `<HHMMSS>_sd.mp4` | `<HHMMSS>_cloud.mp4` |

**配置**：把 [`examples/configuration.yaml`](examples/configuration.yaml) 里的
`ezviz_cloud_pull` / `ezviz_cloud_pull_check` 两个子项合并进 `shell_command:`，检查配置并重启 HA。
自动化可照 [`examples/automation_sd_pull.yaml`](examples/automation_sd_pull.yaml) 复制一份，
把 `shell_command.ezviz_sd_pull` 换成 `shell_command.ezviz_cloud_pull`、事件延迟由 180 秒缩到 **90 秒**。

**验收**：

```bash
sudo python3 /config/custom_components/ezviz_lock/tools/ezviz_cloud_pull.py --dry-run --hours 24
tail -f /config/logs/ezviz_cloud_pull.log
```

成品是 `h264 High 1504x1968 @15fps + aac LC 16kHz mono`，逐帧绿占比 0% 才登记白名单。
集成侧 `_clip_rank()` 把 `_cloud.mp4` 排在 `_sd.mp4` 之前：同一条开门记录先挂 SD 片，
云片补进来后**自动升级**，没有云片的记录保持原样，绝不用别时段的片顶替。

> **已知边界**：云回放流同样是 29.92fps 双路交织，A 路 0% 绿、B 路 92~100% 绿，
> 本脚本只保留 A 路（门外主画面）。B 路成因未验证，详见项目知识库 §22.6。
> 云存储索引接口对长跨度查询会截断，脚本已按 6 小时分片查询合并，不要改回单次大范围查询。

---

## 7. 服务一览

| 服务 | 作用 |
|---|---|
| `ezviz_lock.refresh` | 立即拉一次开门记录，不等轮询 |
| `ezviz_lock.download_clip` | 下载指定开门事件的录像，返回可播放地址 |
| `ezviz_lock.get_playback_url` | 返回指定事件的可播放地址（优先本地 mp4） |
| `ezviz_lock.refetch_loiter` | 把没拿到录像（含被判占位图）的徘徊事件重新排队 |
| `ezviz_lock.delete_loiter` | 删除一条徘徊记录 + 它的录像/抓拍图，留墓碑防云端回看复活 |

`download_clip` / `get_playback_url` / `refetch_loiter` / `delete_loiter` 支持 `supports_response`，
在开发者工具里调用会直接返回结果。多设备时用 `config_entry_id` 指定条目。

---

## 8. 排障

| 现象 | 原因 | 处置 |
|---|---|---|
| 集成里没有「人员 / 开锁方式」 | AppKey 没开 `keylock/open/list` 白名单 | 联系萤石开放平台开通；期间只有告警与抓拍 |
| 录像大片纯绿 | 走的是旧链路 / 未拆流 | 确认 `_sd.mp4` 来自 `ezviz_sd_pull.py`，且 `clean_clips.json` 里 `green_ratio` 为 0 |
| 视频列显示「—」 | 该时段 SD 段还没补到，或偏差超窗（开门 ±30s / 徘徊 ±45s） | 等定时任务补齐；**宁可显示抓拍图也不贴错片** |
| 回放 URL 404 / 0 字节 | 取流太密触发冷却 | 别手动连拉，等 15~20 分钟；脚本会写 `.pull_failures.json` 降权 |
| 拿到 512×288 提示图 | 段还没封口就取流 | 事件触发延迟 180s 就是为此；别调小 |
| `--check` 报 ffmpeg 能力不足 | 容器里 ffmpeg 太旧 | 单文件静态版放 `/config/bin/ffmpeg`，不用重启 HA |
| 面板空白 / 报未知卡片 | 资源没注册或没强刷 | 复查第 5 节两步，Ctrl+F5 |
| 实体 ID 对不上 | 设备名不同 | 开发者工具 → 状态里查实际 ID，替换示例占位符 |

日志：`设置 → 系统 → 日志` 搜 `ezviz_lock`；补录脚本日志在 `/config/logs/ezviz_sd_pull.log`（5MB 轮转）。

---

## 9. 升级 / 卸载 / 回滚

- **升级**：HACS 里点更新 → 重启 HA → **重新执行第 5 节第 1 步的 `cp`**（`/config/www/` 那份不在 HACS 管理范围）。
- **停用补录**：在「设置 → 自动化」停用 `门锁 SD 录像补录`，或删掉 `configuration.yaml` 里的 `shell_command` 块再重启。已经拉好的 mp4 不受影响。
- **卸载集成**：HACS 里移除 → 重启 HA。已生成的抓拍图 / 录像仍在 `/media/tpcamera/door-lock/`，按需自行清理。
- **回滚配置**：改动前先备份 `configuration.yaml` / `automations.yaml`；脚本本身只写媒体文件与 `clean_clips.json`，不改 HA 配置、不重启 HA。

---

## 10. 已知限制

1. HACS 只复制 `custom_components/`，**前端 JS 需一次手动复制**（第 5 节）；没有采用「集成自动注册静态路径」这种未在真机验证过的方案。
2. 录像默认落 `/media/tpcamera/door-lock/`（沿用原系统目录名），可用 `EZVIZ_DEST` 改。
3. SD 回放依赖设备在线 + 云通道可用；偶发 `404` / `20008 设备响应超时`，靠低频 + 降权重试兜底。
4. 转码单段约 60~90 秒占用 CPU；30 分钟一次对 HA 整体负载的影响**未量化**（推测较小，但未实测）。
5. 集成使用 HA 内部存储 `.storage/core.config_entries` 作为脚本凭据来源，若未来 HA 改变该文件结构，请改用 `EZVIZ_APP_KEY` 等环境变量。
6. 徘徊事件没有独立 `event` 实体，自动化靠「计数增加」近似触发，一轮内多条徘徊只触发一次。
7. 质量清单覆盖情况：已具备 `ConfigFlow` + `OptionsFlow`、`ConfigEntry.runtime_data`、
   `strings.json` / `translations/{en,zh-Hans}.json`、`unique_id`、`has_entity_name`、
   服务与实体翻译；**未覆盖** `brand` 官方品牌库收录、完整单元测试集、`config_flow` 的 reauth 流程。

---

## 许可

[MIT](LICENSE) · 欢迎提 Issue / PR。

> 本项目与萤石（EZVIZ）官方无关，是社区自定义集成。使用前请确认遵守萤石开放平台的服务条款。
