/*
 * EZVIZ 门锁 · 总览卡（单卡整体布局）
 * 结构：顶部统计条 → Tab（开门记录 / 徘徊录像）→ 左大图 + 右列表
 * 交互：
 *  - 开门：点右侧任意一行，左侧大图切换成该次开门的画面
 *  - 徘徊：点任意一行，左侧直接变成那一段录像的播放器（有录像时）
 * 数据：sensor 属性 recent_events / today_events / loiter_events（集成提供），无需额外接口。
 */

const TAG = "ezviz-door-lock-panel";

const STYLES = `
:host { display: block; }
ha-card { overflow: hidden; }
.wrap { padding: 14px; box-sizing: border-box; max-width: 1180px; margin: 0 auto; }

.stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 12px; }
.stat { background: var(--secondary-background-color, rgba(127,127,127,.08)); border-radius: 12px; padding: 10px 6px; text-align: center; cursor: pointer; }
.stat:hover { filter: brightness(.97); }
.stat .k { font-size: 12px; color: var(--secondary-text-color); white-space: nowrap; }
.stat .v { font-size: 22px; font-weight: 600; line-height: 1.2; margin-top: 4px; font-variant-numeric: tabular-nums; }
.stat .u { font-size: 11px; color: var(--secondary-text-color); }

.tabs { display: flex; gap: 6px; margin-bottom: 12px; border-bottom: 1px solid var(--divider-color); }
.tab { padding: 8px 14px; font-size: 14px; color: var(--secondary-text-color); cursor: pointer;
       border: none; background: none; font-family: inherit; border-bottom: 2px solid transparent;
       display: flex; align-items: center; gap: 6px; }
.tab:hover { color: var(--primary-text-color); }
.tab.on { color: var(--primary-color); border-bottom-color: var(--primary-color); font-weight: 600; }
.tab .cnt { font-size: 11px; background: var(--secondary-background-color, rgba(127,127,127,.12));
            border-radius: 9px; padding: 1px 6px; font-variant-numeric: tabular-nums; }

.grid { display: grid; grid-template-columns: minmax(0, 1.05fr) minmax(0, 1fr); gap: 16px; align-items: start; }

.hd { text-align: center; margin: 2px 0 10px; }
.hd .k { font-size: 12px; letter-spacing: 1px; color: var(--secondary-text-color); }
.hd .n { font-size: 30px; font-weight: 600; line-height: 1.2; margin-top: 4px; }
.hd .o { font-size: 14px; color: var(--primary-color); margin-top: 2px; }
.hd .t { font-size: 12px; color: var(--secondary-text-color); margin-top: 4px; font-variant-numeric: tabular-nums; }

.box { border-radius: 12px; overflow: hidden; background: var(--secondary-background-color, rgba(127,127,127,.08));
       display: flex; align-items: center; justify-content: center; min-height: 160px; }
.box img { width: 100%; display: block; }
.box video { width: 100%; display: block; background: #000; max-height: 62vh; }
.empty { padding: 44px 12px; text-align: center; color: var(--secondary-text-color); font-size: 13px; line-height: 1.8; }

.meta { text-align: center; margin-top: 10px; }
.meta .m2 { font-size: 12px; color: var(--secondary-text-color); margin-top: 4px; line-height: 1.7; }
.meta a { color: var(--primary-color); text-decoration: none; font-size: 12px; }
.meta a:hover { text-decoration: underline; }

.tools { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin-bottom: 14px; }
.btn { display: flex; flex-direction: column; align-items: center; gap: 4px; padding: 9px 4px; border-radius: 10px;
       border: 1px solid var(--divider-color); background: transparent; color: var(--primary-text-color);
       cursor: pointer; font-size: 12px; font-family: inherit; line-height: 1.2; }
.btn:hover { background: var(--secondary-background-color, rgba(127,127,127,.08)); }
.btn:active { transform: scale(.97); }
.btn ha-icon { --mdc-icon-size: 22px; color: var(--primary-color); }

.rowhead { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 8px; }
.rowhead b { font-size: 15px; }
.rowhead a { font-size: 12px; color: var(--primary-color); text-decoration: none; }
.rowhead a:hover { text-decoration: underline; }

.rows { display: flex; flex-direction: column; gap: 2px; }
.row { display: grid; grid-template-columns: 62px minmax(0, 1fr) auto auto; gap: 8px; align-items: center;
       padding: 6px; border-radius: 10px; cursor: pointer; border-left: 3px solid transparent; transition: background .12s; }
.row:hover { background: var(--secondary-background-color, rgba(127,127,127,.08)); }
.row.on { background: rgba(var(--rgb-primary-color, 3, 169, 244), .12); border-left-color: var(--primary-color); }
.row.delable { grid-template-columns: 62px minmax(0, 1fr) auto auto 26px; }
.del { width: 24px; height: 24px; padding: 0; border: none; border-radius: 6px; background: transparent;
       color: var(--secondary-text-color); cursor: pointer; display: flex; align-items: center; justify-content: center; }
.del:hover { color: var(--error-color, #e53935); background: rgba(229, 57, 53, .12); }
.del ha-icon { --mdc-icon-size: 16px; }
.miniplay { display: inline-flex; align-items: center; gap: 2px; margin-top: 3px; font-size: 10px;
            color: var(--primary-color); text-decoration: none; }
.miniplay:hover { text-decoration: underline; }
.miniplay ha-icon { --mdc-icon-size: 12px; }
.thumb { position: relative; width: 62px; height: 46px; border-radius: 6px; overflow: hidden;
         background: var(--secondary-background-color, rgba(127,127,127,.08));
         display: flex; align-items: center; justify-content: center; font-size: 14px; color: var(--secondary-text-color); }
.thumb img, .thumb video { width: 100%; height: 100%; object-fit: cover; display: block; background: #000; }
.thumb .play { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
               background: rgba(0,0,0,.28); color: #fff; }
.thumb .play ha-icon { --mdc-icon-size: 24px; filter: drop-shadow(0 1px 2px rgba(0,0,0,.6)); }
.thumb .dur { position: absolute; right: 2px; bottom: 2px; font-size: 9px; color: #fff;
              background: rgba(0,0,0,.6); border-radius: 3px; padding: 0 3px; font-variant-numeric: tabular-nums; }
.tm { font-size: 13px; font-weight: 600; font-variant-numeric: tabular-nums; }
.sub { font-size: 11px; color: var(--secondary-text-color); margin-top: 2px; }
.who { font-size: 13px; white-space: nowrap; }
.how { font-size: 11px; color: var(--secondary-text-color); white-space: nowrap; }
.note { font-size: 11px; color: var(--secondary-text-color); margin-top: 10px; line-height: 1.6; }
.nogood { padding: 30px 10px; text-align: center; color: var(--secondary-text-color); font-size: 13px; line-height: 1.8; }
.badge { font-size: 10px; border-radius: 4px; padding: 1px 5px; margin-left: 4px; white-space: nowrap; }
.badge.ready { background: rgba(var(--rgb-success-color, 76, 175, 80), .16); color: var(--success-color, #4caf50); }
.badge.wait { background: rgba(var(--rgb-warning-color, 255, 193, 7), .18); color: var(--warning-color, #ff9800); }
.badge.none { background: var(--secondary-background-color, rgba(127,127,127,.12)); color: var(--secondary-text-color); }

@media (max-width: 760px) {
  .grid { grid-template-columns: minmax(0, 1fr); }
  .hd .n { font-size: 24px; }
  .box img { max-height: 46vh; object-fit: contain; }
  .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
`;

const pad = (n) => String(n).padStart(2, "0");

function fmtFull(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function fmtShort(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso).slice(0, 19).replace("T", " ");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function fmtBytes(n) {
  if (!n) return "";
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

function srcLabel(s, delay) {
  if (s === "alarm") return "告警原图（事发当刻）";
  if (s === "cache") return "告警原图（本地存档）";
  if (s === "capture") return delay ? `事件后 ${delay} 秒补拍` : "事件后补拍";
  return "抓拍图";
}

function srcShort(s) {
  if (s === "alarm" || s === "cache") return "🖼 原图";
  if (s === "capture") return "📷 补拍";
  return "";
}

const esc = (s) =>
  String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");

const keyOf = (e) => String(e.open_time || "") + "|" + String(e.user_name || "");
const keyLo = (e) => String(e.time_ms || e.time || "");

/* 开门录像匹配窗口：SD 段起点可能比开门记录早/晚几秒，±90s 内取最近的一段。
   再宽就会把不相干时段的录像贴到这次开门上，属于造假，不做。 */
const CLIP_WINDOW_MS = 90 * 1000;

function eventMs(e) {
  if (e && e.open_time_ms) return Number(e.open_time_ms);
  const t = e && e.open_time ? new Date(e.open_time).getTime() : NaN;
  if (!isNaN(t)) return t;
  const m = String((e && e.open_time) || "").match(
    /(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})/
  );
  return m
    ? new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]).getTime()
    : NaN;
}

/* 徘徊录像状态 → 展示文案 */
function clipState(lo) {
  if (lo.clip_url) {
    return lo.clip_status === "ready_flv"
      ? { cls: "wait", txt: "flv 存档", playable: false }
      : { cls: "ready", txt: "可播放", playable: true };
  }
  if (lo.clip_status === "recording") return { cls: "wait", txt: "抓录中", playable: false };
  if (lo.clip_status === "pending") return { cls: "wait", txt: "录制中", playable: false };
  if (lo.clip_status === "too_late") return { cls: "none", txt: "已过期", playable: false };
  if (lo.clip_status === "failed") return { cls: "wait", txt: "取流失败", playable: false };
  if (lo.clip_status === "placeholder") return { cls: "none", txt: "无有效录像", playable: false };
  if (lo.clip_status === "no_stream") return { cls: "none", txt: "无录像", playable: false };
  return { cls: "none", txt: "无录像", playable: false };
}

class EzvizDoorLockPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._list = [];       // 开门记录
    this._loiter = [];     // 徘徊记录
    this._clips = [];      // 逐帧验收 0% 绿的开门录像池（sensor.sd_clips）
    this._clipMap = new Map(); // 开门记录 key -> 匹配到的干净录像
    this._sel = null;
    this._selLo = null;
    this._tab = "open";
    this._sig = null;
    this._sigLo = null;
    this._lastSrc = undefined;
    this._lastClip = undefined;
    this._built = false;
  }

  static getStubConfig() {
    return { entity: "sensor.y3000fvx_plus_yourserial_opens_today" };
  }

  setConfig(config) {
    if (!config || !config.entity) throw new Error("需要配置 entity（今日开门次数传感器）");
    this._config = Object.assign({ list: "recent_events", max: 30 }, config);
    this._entity = config.entity;
    this._listKey = this._config.list === "today_events" ? "today_events" : "recent_events";
    this._max = Number(this._config.max) || 30;
    this._build();
    if (this._hass) this._onHass(this._hass, true);
  }

  getCardSize() {
    return 16;
  }

  getGridOptions() {
    return { columns: 12, rows: "auto", min_columns: 6 };
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._entity || !this._built) return;
    try {
      this._onHass(hass, false);
    } catch (err) {
      console.error("[ezviz-door-lock-panel]", err);
    }
  }

  _build() {
    this._built = true;
    this.shadowRoot.innerHTML = `
      <style>${STYLES}</style>
      <ha-card><div class="wrap">
        <div class="stats">
          <div class="stat" id="st-open"><div class="k">今日开门</div><div class="v" id="s-open">—</div><div class="u">次</div></div>
          <div class="stat"><div class="k">今日关门</div><div class="v" id="s-close">—</div><div class="u">次</div></div>
          <div class="stat" id="st-lo"><div class="k">今日徘徊</div><div class="v" id="s-lo">—</div><div class="u">次</div></div>
          <div class="stat"><div class="k">本次人数</div><div class="v" id="s-person">—</div><div class="u" id="s-person-u">人</div></div>
        </div>

        <div class="tabs">
          <button class="tab on" data-tab="open"><ha-icon icon="mdi:door-open" style="--mdc-icon-size:16px"></ha-icon>开门记录<span class="cnt" id="c-open">0</span></button>
          <button class="tab" data-tab="loiter"><ha-icon icon="mdi:motion-sensor" style="--mdc-icon-size:16px"></ha-icon>徘徊录像<span class="cnt" id="c-lo">0</span></button>
        </div>

        <div class="grid">
          <div class="left">
            <div class="hd">
              <div class="k" id="hd-k">最近一次开门</div>
              <div class="n" id="hd-n">—</div>
              <div class="o" id="hd-o"></div>
              <div class="t" id="hd-t"></div>
            </div>
            <div class="box" id="box"></div>
            <div class="meta">
              <div class="m2" id="m2"></div>
              <div class="m2" id="m3"></div>
            </div>
          </div>
          <div class="right">
            <div class="tools" id="tools-open">
              <button class="btn" data-act="refresh"><ha-icon icon="mdi:refresh"></ha-icon><span>立即刷新</span></button>
              <button class="btn" data-act="clip"><ha-icon icon="mdi:download"></ha-icon><span>下载录像</span></button>
              <button class="btn" data-act="playback"><ha-icon icon="mdi:play-circle-outline"></ha-icon><span>回放地址</span></button>
            </div>
            <div class="tools" id="tools-loiter" style="display:none">
              <button class="btn" data-act="refresh"><ha-icon icon="mdi:refresh"></ha-icon><span>立即刷新</span></button>
              <button class="btn" data-act="reclip"><ha-icon icon="mdi:video-refresh"></ha-icon><span>重取录像</span></button>
              <button class="btn" data-act="album"><ha-icon icon="mdi:image-multiple"></ha-icon><span>抓拍相册</span></button>
            </div>
            <div class="rowhead">
              <b id="rh-title">最近开门记录</b>
              <a id="rh-link" href="/door-lock/today">今日明细 ›</a>
            </div>
            <div class="rows" id="rows"></div>
            <div class="note" id="note"></div>
          </div>
        </div>
      </div></ha-card>`;

    this.shadowRoot.querySelectorAll(".btn").forEach((b) => {
      b.addEventListener("click", () => this._action(b.dataset.act));
    });
    this.shadowRoot.querySelectorAll(".tab").forEach((t) => {
      t.addEventListener("click", () => this._setTab(t.dataset.tab));
    });
    this.shadowRoot.getElementById("st-open").addEventListener("click", () => this._setTab("open"));
    this.shadowRoot.getElementById("st-lo").addEventListener("click", () => this._setTab("loiter"));
    this.shadowRoot.getElementById("rows").addEventListener("click", (ev) => {
      const del = ev.target.closest(".del");
      if (del) {
        ev.stopPropagation();
        const row = del.closest(".row");
        if (row) this._deleteLoiter(row.dataset.uid, Number(row.dataset.ms) || null);
        return;
      }
      // 录像小链接：让它自己开新标签，别顺带把这一行切成选中态
      if (ev.target.closest("a.miniplay")) {
        ev.stopPropagation();
        return;
      }
      const row = ev.target.closest(".row");
      if (!row) return;
      const k = row.dataset.k;
      if (!k) return;
      if (this._tab === "loiter") {
        if (k === this._selLo) return;
        this._selLo = k;
        this._paintRows();
        this._paintShot();
        return;
      }
      if (k === this._sel) return;
      this._sel = k;
      this._paintRows();
      this._paintShot();
    });
  }

  _setTab(tab) {
    if (this._tab === tab) return;
    this._tab = tab;
    this.shadowRoot.querySelectorAll(".tab").forEach((t) => {
      t.classList.toggle("on", t.dataset.tab === tab);
    });
    this.shadowRoot.getElementById("tools-open").style.display = tab === "open" ? "" : "none";
    this.shadowRoot.getElementById("tools-loiter").style.display = tab === "loiter" ? "" : "none";
    const ttl = this.shadowRoot.getElementById("rh-title");
    const lnk = this.shadowRoot.getElementById("rh-link");
    if (tab === "loiter") {
      ttl.textContent = "徘徊唤醒记录";
      lnk.textContent = "抓拍相册 ›";
      lnk.href = "/door-lock/album";
    } else {
      ttl.textContent = "最近开门记录";
      lnk.textContent = "今日明细 ›";
      lnk.href = "/door-lock/today";
    }
    this._sig = null;
    this._sigLo = null;
    this._paintRows();
    this._paintShot();
  }

  _onHass(hass, force) {
    const st = hass.states[this._entity];
    const a = (st && st.attributes) || {};
    const raw = Array.isArray(a[this._listKey]) ? a[this._listKey] : [];
    const list = raw.slice(0, this._max);
    this._list = list;
    this._loiter = Array.isArray(a.loiter_events) ? a.loiter_events.slice(0, this._max) : [];
    // sd_clips 来自集成侧逐帧验收白名单（size+md5 复核过），绿片不在其中
    this._clips = Array.isArray(a.sd_clips) ? a.sd_clips : [];
    this._clipMap = this._matchClips(list, this._clips);

    this.shadowRoot.getElementById("s-open").textContent = st ? st.state : "—";
    this.shadowRoot.getElementById("s-close").textContent = a.today_close_count ?? "—";
    this.shadowRoot.getElementById("s-lo").textContent = a.loiter_today_count ?? "0";
    this.shadowRoot.getElementById("c-open").textContent = String(list.length);
    this.shadowRoot.getElementById("c-lo").textContent = String(this._loiter.length);

    const head = list[0] || {};
    this.shadowRoot.getElementById("s-person").textContent = head.person_count ?? "—";
    this.shadowRoot.getElementById("s-person-u").textContent =
      head.person_count_source && head.person_count_source !== "customer_info" ? "人 · 估算" : "人";

    if (!this._sel || !list.some((e) => keyOf(e) === this._sel)) {
      this._sel = list.length ? keyOf(list[0]) : null;
    }
    if (!this._selLo || !this._loiter.some((e) => keyLo(e) === this._selLo)) {
      this._selLo = this._loiter.length ? keyLo(this._loiter[0]) : null;
    }

    // 列表内容签名：包含图片/录像地址，录像就位后要重绘
    const clipSig = this._clips
      .map((c) => `${c.name}:${c.start_ms}`)
      .join(",");
    const sig =
      list.length +
      "#" + clipSig + "#" +
      list.map((e) => keyOf(e) + (e.snapshot_url ? "1" : "0") + (e.clip_url ? "1" : "0")).join(";");
    const sigLo =
      this._loiter.length +
      "#" +
      this._loiter.map((e) => keyLo(e) + "|" + (e.clip_url ? "1" : "0") + (e.snapshot_url ? "1" : "0")).join(";");

    let dirty = false;
    if (force || sig !== this._sig || sigLo !== this._sigLo) {
      dirty = true;
    }
    this._sig = sig;
    this._sigLo = sigLo;
    if (dirty) this._paintRows();
    this._paintShot();
  }

  /* 把「逐帧验收 0% 绿」的 SD 录像按时间就近匹配到开门记录。
     - 事件自带的 clip_url 只有在白名单里才认（旧集成抓直播的绿片一律忽略）
     - 一段录像只配一条记录，避免同一段贴满整列
     - ±90 秒之外不配，不把不相干时段的画面冒充这次开门 */
  _matchClips(list, clips) {
    const map = new Map();
    const used = new Set();
    // 纵深防御：即便后端白名单出错，绿片 / 不足 15 帧的片也不认
    const pool = clips.filter(
      (c) => c && c.url && Number(c.green_ratio || 0) <= 0.02 && Number(c.frames || 0) >= 15
    );
    const findByUrl = (url) => {
      if (!url) return null;
      const base = String(url).split("?")[0];
      return (
        pool.find((c) => c.url === url) ||
        pool.find((c) => c.url && base.endsWith("/" + c.name)) ||
        null
      );
    };
    for (const e of list) {
      let hit = findByUrl(e.clip_url);
      if (hit && used.has(hit.url)) hit = null;
      if (!hit) {
        const ms = eventMs(e);
        if (!isNaN(ms)) {
          let bestD = Infinity;
          for (const c of pool) {
            if (!c.start_ms || used.has(c.url)) continue;
            const d = Math.abs(ms - Number(c.start_ms));
            if (d <= CLIP_WINDOW_MS && d < bestD) {
              hit = c;
              bestD = d;
            }
          }
        }
      }
      if (hit && hit.url) {
        used.add(hit.url);
        map.set(keyOf(e), hit);
      }
    }
    return map;
  }

  /* ---------------- 列表 ---------------- */
  _paintRows() {
    const box = this.shadowRoot.getElementById("rows");
    const note = this.shadowRoot.getElementById("note");
    if (this._tab === "loiter") {
      this._paintLoiterRows(box, note);
      return;
    }
    this._paintOpenRows(box, note);
  }

  _paintOpenRows(box, note) {
    const list = this._list;
    if (!list.length) {
      box.innerHTML = `<div class="nogood">最近还没有开门记录</div>`;
      note.textContent = "";
      return;
    }
    box.innerHTML = list
      .map((e) => {
        const k = keyOf(e);
        const on = k === this._sel ? " on" : "";
        const clip = this._clipMap.get(k) || null;
        let thumb;
        if (clip) {
          // 缩略图就是这段录像的首帧；#t=0.1 让浏览器在 preload=metadata 时也出画面
          const poster = e.snapshot_url ? ` poster="${esc(e.snapshot_url)}"` : "";
          const dur = clip.duration_s
            ? `<span class="dur">${Math.round(clip.duration_s)}s</span>`
            : "";
          thumb = `<video src="${esc(clip.url)}#t=0.1" preload="metadata" muted playsinline${poster}></video><div class="play"><ha-icon icon="mdi:play-circle"></ha-icon></div>${dur}`;
        } else if (e.snapshot_url) {
          thumb = `<img src="${esc(e.snapshot_url)}" alt="" loading="lazy">`;
        } else {
          thumb = "—";
        }
        const sub = clip ? "▶ 开门录像" : srcShort(e.snapshot_source);
        return `<div class="row${on}" data-k="${esc(k)}" title="${clip ? "点击播放这段开门录像" : "点击查看这次开门的抓拍图"}">
          <div class="thumb">${thumb}</div>
          <div><div class="tm">${fmtShort(e.open_time)}</div>${sub ? `<div class="sub">${esc(sub)}</div>` : ""}</div>
          <div class="who">${esc(e.user_name || "未知")}</div>
          <div class="how">${esc(e.open_type_name || "")}</div>
        </div>`;
      })
      .join("");
    note.textContent = "▶ 标记的记录点开即播放开门录像（逐帧验收、无绿画面）；其余显示抓拍图";
  }

  _paintLoiterRows(box, note) {
    const list = this._loiter;
    if (!list.length) {
      box.innerHTML = `<div class="nogood">还没有徘徊唤醒记录<br><span style="font-size:12px">有人在门口停留、但没开门时，设备被唤醒并录制一段</span></div>`;
      note.textContent = "";
      return;
    }
    box.innerHTML = list
      .map((e) => {
        const k = keyLo(e);
        const on = k === this._selLo ? " on" : "";
        const stt = clipState(e);
        const img = e.snapshot_url
          ? `<img src="${esc(e.snapshot_url)}" alt="" loading="lazy">`
          : "";
        const overlay = stt.playable
          ? `<div class="play"><ha-icon icon="mdi:play-circle"></ha-icon></div>`
          : "";
        const dur = e.duration_s ? `<span class="dur">${e.duration_s}s</span>` : "";
        return `<div class="row delable${on}" data-k="${esc(k)}" data-uid="${esc(e.uid || "")}" data-ms="${esc(e.time_ms || "")}" title="点击查看这一段录像">
          <div class="thumb">${img || "—"}${overlay}${dur}</div>
          <div>
            <div class="tm">${fmtShort(e.time)}</div>
            <div class="sub">${esc(e.alarm_type_name || "徘徊检测")}</div>
          </div>
          <div class="who"><span class="badge ${stt.cls}">${stt.txt}</span></div>
          <div class="how">${esc(fmtBytes(e.clip_bytes))}</div>
          <button class="del" title="删除这条记录及其录像" aria-label="删除这条记录"><ha-icon icon="mdi:trash-can-outline"></ha-icon></button>
        </div>`;
      })
      .join("");
    note.textContent = "点任意一条，左侧直接播放那一段录像；右侧垃圾桶删除该条记录及其录像/抓拍图（不可恢复）";
  }

  /* ---------------- 左栏 ---------------- */
  _cur() {
    return this._list.find((e) => keyOf(e) === this._sel) || this._list[0] || null;
  }

  _curLo() {
    return this._loiter.find((e) => keyLo(e) === this._selLo) || this._loiter[0] || null;
  }

  _paintShot() {
    if (this._tab === "loiter") {
      this._paintLoiterShot();
      return;
    }
    const e = this._cur();
    const box = this.shadowRoot.getElementById("box");
    const hdK = this.shadowRoot.getElementById("hd-k");
    const hdN = this.shadowRoot.getElementById("hd-n");
    const hdO = this.shadowRoot.getElementById("hd-o");
    const hdT = this.shadowRoot.getElementById("hd-t");
    const m2 = this.shadowRoot.getElementById("m2");
    const m3 = this.shadowRoot.getElementById("m3");

    if (!e) {
      this._lastSrc = null;
      box.innerHTML = `<div class="empty">暂无开门记录</div>`;
      hdK.textContent = "最近一次开门";
      hdN.textContent = "—";
      hdO.textContent = "";
      hdT.textContent = "";
      m2.textContent = "";
      m3.innerHTML = "";
      return;
    }

    const url = e.snapshot_url || null;
    const clip = this._clipMap.get(keyOf(e)) || null;
    const clipUrl = clip ? clip.url : null;
    if (clipUrl) {
      // 这条记录匹配到逐帧验收过的干净录像：左栏直接是可播的播放器
      this._lastSrc = null;
      if (this._lastClip !== clipUrl) {
        this._lastClip = clipUrl;
        const poster = url ? ` poster="${esc(url)}"` : "";
        box.innerHTML = `<video controls autoplay muted playsinline preload="metadata"${poster} src="${esc(clipUrl)}"></video>`;
      }
    } else if (url) {
      this._lastClip = null;
      if (this._lastSrc !== url) {
        this._lastSrc = url;
        box.innerHTML = `<a href="${esc(url)}" target="_blank" rel="noopener"><img src="${esc(url)}" alt="开门抓拍"></a>`;
      }
    } else {
      this._lastClip = null;
      this._lastSrc = null;
      box.innerHTML = `<div class="empty">📷 这条记录既没有录像也没有图片<br><span style="font-size:12px">室内按键 / 机械钥匙开门不产生门外画面</span></div>`;
    }

    const head = this._list[0];
    const isLatest = head && keyOf(head) === keyOf(e);
    hdK.textContent = isLatest ? "最近一次开门" : "正在查看";
    hdN.textContent = e.user_name || "未知";
    hdO.textContent = e.open_type_name || "";
    hdT.textContent = fmtFull(e.open_time);

    const bits = [clipUrl ? "开门录像（逐帧验收 0% 绿）" : srcLabel(e.snapshot_source, e.snapshot_delay_s)];
    if (clip && clip.frames) bits.push(`${clip.frames} 帧 / ${clip.duration_s}s`);
    if (e.person_count_source === "default") bits.push("人数为估算");
    m2.textContent = bits.join(" ｜ ");
    const imgLink = url ? `<a href="${esc(url)}" target="_blank" rel="noopener">查看原图 ›</a>` : "";
    m3.innerHTML = clipUrl
      ? `<a href="${esc(clipUrl)}" target="_blank" rel="noopener">在新标签打开录像 ›</a>${imgLink ? " ｜ " + imgLink : ""}`
      : imgLink;
  }

  _paintLoiterShot() {
    const e = this._curLo();
    const box = this.shadowRoot.getElementById("box");
    const hdK = this.shadowRoot.getElementById("hd-k");
    const hdN = this.shadowRoot.getElementById("hd-n");
    const hdO = this.shadowRoot.getElementById("hd-o");
    const hdT = this.shadowRoot.getElementById("hd-t");
    const m2 = this.shadowRoot.getElementById("m2");
    const m3 = this.shadowRoot.getElementById("m3");
    this._lastSrc = undefined;

    if (!e) {
      this._lastClip = null;
      box.innerHTML = `<div class="empty">还没有徘徊唤醒记录</div>`;
      hdK.textContent = "徘徊唤醒";
      hdN.textContent = "—";
      hdO.textContent = "";
      hdT.textContent = "";
      m2.textContent = "";
      m3.innerHTML = "";
      return;
    }

    const stt = clipState(e);
    const head = this._loiter[0];
    const isLatest = head && keyLo(head) === keyLo(e);

    hdK.textContent = isLatest ? "最近一次徘徊" : "正在查看";
    hdN.textContent = "有人徘徊";
    hdO.textContent = e.alarm_type_name || "徘徊检测";
    hdT.textContent = fmtFull(e.time);

    // 有可播录像就放播放器；否则退回抓拍图并说明原因
    const clip = stt.playable ? e.clip_url : null;
    box.style.position = "";
    if (clip) {
      if (this._lastClip !== clip) {
        this._lastClip = clip;
        const poster = e.snapshot_url ? ` poster="${esc(e.snapshot_url)}"` : "";
        box.innerHTML = `<video controls autoplay muted playsinline preload="metadata"${poster} src="${esc(clip)}"></video>`;
      }
    } else {
      this._lastClip = null;
      const img = e.snapshot_url
        ? `<a href="${esc(e.snapshot_url)}" target="_blank" rel="noopener"><img src="${esc(e.snapshot_url)}" alt="徘徊抓拍"></a>`
        : "";
      let why = "设备没有录到这一段";
      if (e.clip_status === "recording") why = "正在从直播流抓录，约 15 秒后自动出现";
      else if (e.clip_status === "too_late") why = "事件已经过去太久，直播流不在那一刻，补不回来了";
      else if (e.clip_status === "pending") why = "设备正在录制，稍后自动补上（约 30~60 秒）";
      else if (e.clip_status === "failed") why = "取流失败，可点右侧「重取录像」再试一次";
      else if (e.clip_status === "ready_flv") why = "已存为 flv 存档，浏览器无法直接播放";
      else if (e.clip_status === "placeholder") why = "平台只返回了占位提示画面（编码不匹配），不是真实录像";
      else if (e.clip_status === "no_stream") why = "这一段没有拿到录像";
      if (img) {
        box.style.position = "relative";
        box.innerHTML = `${img}<div style="position:absolute;bottom:0;left:0;right:0;padding:8px;background:rgba(0,0,0,.55);color:#fff;font-size:12px">${esc(why)}</div>`;
      } else {
        box.innerHTML = `<div class="empty">🎬 暂无录像<br><span style="font-size:12px">${esc(why)}</span></div>`;
      }
    }

    m2.textContent = `录像时长 ${e.duration_s || 30} 秒 ｜ ${stt.txt}${e.clip_bytes ? " ｜ " + fmtBytes(e.clip_bytes) : ""}`;
    m3.innerHTML = e.clip_url
      ? `<a href="${esc(e.clip_url)}" target="_blank" rel="noopener">在新标签打开录像 ›</a>`
      : "";
  }

  _deleteLoiter(uid, timeMs) {
    const hass = this._hass;
    const notify = (message) =>
      this.dispatchEvent(
        new CustomEvent("hass-notification", { detail: { message }, bubbles: true, composed: true })
      );
    if (!hass) return;
    const item = this._loiter.find((e) =>
      uid ? String(e.uid || "") === String(uid) : Number(e.time_ms) === Number(timeMs)
    );
    const when = item ? fmtFull(item.time) : timeMs ? String(timeMs) : "未知时间";
    if (
      !window.confirm(
        `确定删除这条徘徊记录吗？\n\n时间：${when}\n\n该条记录、对应的录像和抓拍图会一起删除，不可恢复；云端再次查到这条告警也不会重新出现。`
      )
    ) {
      return;
    }
    const data = {};
    if (uid) data.uid = String(uid);
    else if (timeMs) data.time_ms = Number(timeMs);
    else {
      notify("无法定位这条记录，删除已取消");
      return;
    }
    hass
      .callService("ezviz_lock", "delete_loiter", data)
      .then((r) => {
        const res = (r && r.response) || {};
        // 部分 HA 版本的 callService 不带回 response：只要没 reject 就当删除成功
        if (res.deleted === false) {
          notify(res.error || "删除失败，记录可能已不存在");
          return;
        }
        this._loiter = this._loiter.filter((e) =>
          uid ? String(e.uid || "") !== String(uid) : Number(e.time_ms) !== Number(timeMs)
        );
        this._selLo = this._loiter.length ? keyLo(this._loiter[0]) : null;
        this._sigLo = null;
        this._paintRows();
        this._paintShot();
        notify(res.files ? `已删除该条记录与 ${res.files} 个媒体文件` : "已删除该条徘徊记录");
      })
      .catch((err) => notify(`删除失败：${(err && err.message) || err}`));
  }

  _action(act) {
    const hass = this._hass;
    if (!hass) return;
    const done = (message) =>
      this.dispatchEvent(
        new CustomEvent("hass-notification", { detail: { message }, bubbles: true, composed: true })
      );

    if (act === "refresh") {
      hass.callService("ezviz_lock", "refresh", {});
      done("已请求刷新，稍等几秒");
    } else if (act === "clip") {
      // 只交付逐帧验收过的干净录像；不再触发旧「抓直播」路径，避免生成绿屏 mp4
      const ev = this._cur();
      const clip = ev && this._clipMap.get(keyOf(ev));
      if (clip && clip.url) {
        window.open(clip.url, "_blank", "noopener");
        done("已在新标签打开这段干净开门录像（可右键另存）");
      } else {
        done("这条记录没有逐帧验收过的录像，未启动旧抓录（避免生成绿屏文件）");
      }
    } else if (act === "playback") {
      hass.callService("ezviz_lock", "get_playback_url", { index: 0 });
      done("已请求回放地址，见通知");
    } else if (act === "reclip") {
      hass
        .callService("ezviz_lock", "refetch_loiter", {})
        .then((r) => {
          const n = (r && r.response && r.response.queued) || 0;
          done(n ? `已重新排队 ${n} 条徘徊录像，稍等片刻` : "没有需要重取的徘徊录像");
        })
        .catch(() => {
          hass.callService("ezviz_lock", "refresh", {});
          done("已请求刷新，稍等几秒");
        });
    } else if (act === "album") {
      window.history.pushState(null, "", "/door-lock/album");
      window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
    }
  }
}

if (!customElements.get(TAG)) customElements.define(TAG, EzvizDoorLockPanel);

window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === TAG)) {
  window.customCards.push({
    type: TAG,
    name: "EZVIZ 门锁总览",
    description: "开门记录 + 徘徊录像，点记录切换左侧画面 / 播放录像",
    preview: false,
  });
}
