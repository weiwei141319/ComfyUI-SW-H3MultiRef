/**
 * SW 海螺H3 · 多图参考素材输入（SWH3MultiRefSegments）—— 导演台形态的面板
 *
 * 形态（与 SW 万相2 素材面板 / LTX2.5 导演台同款）：
 *   · 画布上是一个小节点：只显示「N 段 · M 段完整 · 预计总时长」和「打开素材面板」按钮。
 *   · 点开后弹出大面板，所有素材输入都在面板里完成，画布上不再拖 LoadVideo / LoadImage / LoadAudio。
 *   · 每段一张卡片，四列：
 *       ① 参考视频（1 段，含「取帧设置」：force_rate / frame_load_cap / skip_first_frames /
 *          select_every_nth / custom_width / custom_height / 格式 / 用视频原声）
 *          —— 取帧语义与 SW-loadvideo（VHS_LoadVideo）一致
 *       ② 参考图（最多 9 张，3×3 网格）
 *       ③ 参考音频（最多 3 条）
 *       ④ 提示词 + 使用时长（秒）
 *
 * 数据：整份段位数据序列化成 JSON，存在节点上隐藏的「段位数据」STRING 控件里；
 *      下游「多图参考生视频（一体）」节点只看输出的一根 SW_H3_MULTIREF 线。
 *
 * 素材来源：
 *   · 下拉框选 ComfyUI input 目录里已有的文件（走 /sw_h3ref/input_files）；
 *   · 或直接上传（走 /upload/image，写到 input/sw_h3_multiref/）。
 *
 * 验证办法（F12 控制台）：应看到 [SW-H3MultiRef] 扩展已加载；没有 = 浏览器缓存旧页面 → Ctrl+F5。
 *
 * ⚠ 本文件必须在 web/ 顶层（不能放 web/js/），否则相对 import 会 404 → 扩展静默不加载。
 */
import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";

const NODE_NAME = "SWH3MultiRefSegments";
const DATA_WIDGET = "段位数据";
const MAX_SLOTS = 12;
const MAX_IMAGES = 9;
const MAX_AUDIOS = 3;
const UPLOAD_SUBFOLDER = "sw_h3_multiref";
const MAX_UPLOAD_BYTES = 1024 * 1024 * 1024; // 1 GiB
const STYLE_ID = "sw-h3multiref-style";
const PANEL_WIDGET = "sw_h3_multiref_panel";

const FORMATS = ["H3", "AnimateDiff", "Wan", "LTXV", "Mochi", "Hunyuan", "Cosmos", "None"];

const DEFAULT_VIDEO_OPTS = {
  force_rate: 24,
  frame_load_cap: 0,
  skip_first_frames: 0,
  select_every_nth: 1,
  custom_width: 0,
  custom_height: 0,
  format: "H3",
  use_audio: true,
};

// 全局只允许一个打开的面板：切工作流/重载后旧面板会被自动关闭，避免"幽灵面板"拦截点击。
const ACTIVE_DIALOG = { ui: null };

const LOG = (...args) => console.log("[SW-H3MultiRef]", ...args);
LOG("扩展已加载");

/* ------------------------------ 小工具 ------------------------------ */

const uid = () => `${Date.now()}_${Math.random().toString(36).slice(2, 9)}`;

function escapeHTML(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function viewURL(relative) {
  const parts = String(relative).replace(/\\/g, "/").split("/");
  const filename = parts.pop() ?? "";
  const params = new URLSearchParams({ filename, type: "input" });
  if (parts.length) params.set("subfolder", parts.join("/"));
  return api.apiURL(`/view?${params.toString()}`);
}

const num = (value, fallback = 0) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

function fmtSeconds(value) {
  const n = num(value, 0);
  return n > 0 ? `${n.toFixed(2)}s` : "—";
}

function normaliseItems(list, limit) {
  const out = [];
  for (const entry of Array.isArray(list) ? list.slice(0, limit) : []) {
    const file = String((entry && entry.file) || entry || "").trim();
    if (!file) continue;
    out.push({ file, name: String((entry && entry.name) || file.split("/").pop()) });
  }
  return out;
}

function normaliseVideoOpts(raw) {
  const source = raw && typeof raw === "object" ? raw : {};
  const opts = { ...DEFAULT_VIDEO_OPTS };
  for (const key of ["force_rate", "skip_first_frames", "custom_width", "custom_height"]) {
    opts[key] = num(source[key], DEFAULT_VIDEO_OPTS[key]);
  }
  opts.frame_load_cap = num(source.frame_load_cap, 0);
  opts.select_every_nth = Math.max(1, num(source.select_every_nth, 1));
  opts.format = FORMATS.includes(source.format) ? source.format : DEFAULT_VIDEO_OPTS.format;
  opts.use_audio = source.use_audio === undefined ? true : !!source.use_audio;
  return opts;
}

function normaliseState(raw) {
  const segments = Array.isArray(raw?.segments) ? raw.segments : [];
  return {
    version: 1,
    segments: segments.slice(0, MAX_SLOTS).map((segment) => ({
      id: String(segment?.id || uid()),
      video: String(segment?.video || ""),
      videoName: String(segment?.videoName || segment?.video || ""),
      duration: num(segment?.duration, 0),
      fps: num(segment?.fps, 0),
      videoOpts: normaliseVideoOpts(segment?.videoOpts),
      images: normaliseItems(segment?.images, MAX_IMAGES),
      audios: normaliseItems(segment?.audios, MAX_AUDIOS),
      prompt: String(segment?.prompt || ""),
      seconds: num(segment?.seconds, 0),
    })),
  };
}

function newSegment(patch = {}) {
  return {
    id: uid(),
    video: "",
    videoName: "",
    duration: 0,
    fps: 0,
    videoOpts: { ...DEFAULT_VIDEO_OPTS },
    images: [],
    audios: [],
    prompt: "",
    seconds: 0,
    ...patch,
  };
}

function chooseFiles(accept, multiple = false) {
  return new Promise((resolve) => {
    const picker = document.createElement("input");
    picker.type = "file";
    picker.accept = accept;
    picker.multiple = multiple;
    picker.hidden = true;
    picker.onchange = () => resolve([...(picker.files || [])]);
    picker.oncancel = () => resolve([]);
    document.body.appendChild(picker);
    picker.click();
    setTimeout(() => picker.remove(), 120000);
  });
}

async function uploadFile(file) {
  if (!file || file.size <= 0) throw new Error("文件为空");
  if (file.size > MAX_UPLOAD_BYTES) throw new Error("单个文件超过 1 GiB");
  const safeName = file.name.replace(/[^\w.()\-\u4e00-\u9fff]+/gu, "_");
  const serverName = `${Date.now()}_${Math.random().toString(36).slice(2, 7)}_${safeName}`;
  const data = new FormData();
  data.append("image", file, serverName);
  data.append("type", "input");
  data.append("subfolder", UPLOAD_SUBFOLDER);
  data.append("overwrite", "true");
  const response = await api.fetchApi("/upload/image", { method: "POST", body: data });
  let payload = {};
  try {
    payload = await response.json();
  } catch (_) { /* 非 JSON 响应忽略 */ }
  if (!response.ok) throw new Error(payload?.error || `上传失败：HTTP ${response.status}`);
  return [payload?.subfolder, payload?.name].filter(Boolean).join("/");
}

async function probe(kind, relative) {
  if (!relative) return null;
  try {
    const response = await api.fetchApi(`/sw_h3ref/probe_${kind}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: relative }),
    });
    if (!response.ok) return null;
    const data = await response.json();
    return data && !data.error ? data : null;
  } catch (error) {
    LOG(`probe_${kind} 失败:`, error);
    return null;
  }
}

async function listInputFiles() {
  const empty = { videos: [], images: [], audios: [] };
  try {
    const response = await api.fetchApi("/sw_h3ref/input_files");
    if (!response.ok) return empty;
    const data = await response.json();
    return {
      videos: data?.videos || [],
      images: data?.images || [],
      audios: data?.audios || [],
    };
  } catch (error) {
    LOG("列目录失败:", error);
    return empty;
  }
}

function optionsHTML(items, selected) {
  const current = String(selected || "");
  const seen = new Set();
  let html = `<option value="">— 未选择 —</option>`;
  for (const item of items) {
    const value = String(item?.path || "");
    if (!value || seen.has(value)) continue;
    seen.add(value);
    const label = String(item?.name || value);
    html += `<option value="${escapeHTML(value)}"${value === current ? " selected" : ""}>${escapeHTML(label)}</option>`;
  }
  // 值不在列表里（input 目录被清理过）也保留，避免静默丢失
  if (current && !seen.has(current)) {
    html += `<option value="${escapeHTML(current)}" selected>${escapeHTML(current)}（缺失）</option>`;
  }
  return html;
}

/* ------------------------------ 面板 ------------------------------ */

class SWH3MultiRefUI {
  constructor(node, widget, root) {
    this.node = node;
    this.widget = widget;
    this.root = root;
    this.state = normaliseState(this.read());
    this.dialog = null;
    this.status = "就绪";
    this.busy = false;
    this.files = { videos: [], images: [], audios: [] };
    this.renderNode();
  }

  read() {
    try {
      return JSON.parse(this.widget?.value || "{}");
    } catch (_) {
      return { version: 1, segments: [] };
    }
  }

  sync() {
    this.widget.value = JSON.stringify(this.state);
    this.widget.callback?.(this.widget.value);
    this.node.graph?.setDirtyCanvas?.(true, true);
    this.renderNode();
  }

  setStatus(text) {
    this.status = text;
    if (this.dialog?.open) {
      const target = this.dialog.querySelector(".swh3-status");
      if (target) target.textContent = text;
    }
    this.renderNode();
  }

  totals() {
    const segments = this.state.segments;
    const complete = segments.filter((s) => (s.video || s.images.length || s.audios.length) && s.prompt.trim()).length;
    const seconds = segments.reduce((sum, s) => sum + num(s.seconds, 0), 0);
    return { count: segments.length, complete, seconds };
  }

  /* ---------------------- 画布上的小节点 ---------------------- */

  renderNode() {
    const { count, complete, seconds } = this.totals();
    this.root.innerHTML = `
      <div class="swh3-node">
        <div class="swh3-node-title">SW 海螺H3 · 多图参考素材输入</div>
        <div class="swh3-node-summary">${count} 段 · ${complete} 段就绪 · 预计总时长 ${seconds.toFixed(1)}s</div>
        <button class="swh3-button primary" data-action="open">打开素材面板</button>
        <div class="swh3-node-hint">${escapeHTML(this.status)}</div>
      </div>`;
    this.root.querySelector('[data-action="open"]')?.addEventListener("click", () => this.open());
  }

  /* ---------------------- 大面板 ---------------------- */

  async open() {
    if (ACTIVE_DIALOG.ui && ACTIVE_DIALOG.ui !== this) ACTIVE_DIALOG.ui.closeDialog();
    if (!this.dialog) {
      this.dialog = document.createElement("dialog");
      this.dialog.className = "swh3-modal";
      document.body.appendChild(this.dialog);
      this.dialog.addEventListener("close", () => {
        // 节点被移除时 destroy() 会先把 this.dialog 置空，而 close 是异步派发的，这里要取局部变量。
        const dialog = this.dialog;
        if (dialog) dialog.removeAttribute("open");
        if (ACTIVE_DIALOG.ui === this) ACTIVE_DIALOG.ui = null;
      });
      // 事件委托：面板重渲染后无需重新绑定
      this.dialog.addEventListener("click", (event) => this.onClick(event));
      this.dialog.addEventListener("input", (event) => this.onInput(event));
      this.dialog.addEventListener("change", (event) => this.onChange(event));
    }
    this.renderDialog();
    ACTIVE_DIALOG.ui = this;
    if (!this.dialog.open) this.dialog.showModal();

    this.setStatus("正在读取 input 目录…");
    this.files = await listInputFiles();
    this.setStatus(`input 目录：${this.files.videos.length} 视频 / ${this.files.images.length} 图片 / ${this.files.audios.length} 音频`);
    this.renderDialog();
    await this.probeMissing();
  }

  /** 打开面板时补读还没探测过的视频 / 音频信息。 */
  async probeMissing() {
    const pendingVideo = this.state.segments.filter((s) => s.video && !(s.duration > 0));
    for (let i = 0; i < pendingVideo.length; i++) {
      if (!this.dialog?.open) return;
      const segment = pendingVideo[i];
      this.setStatus(`读取视频信息 ${i + 1}/${pendingVideo.length}：${segment.videoName}`);
      const info = await probe("video", segment.video);
      if (info) {
        segment.duration = num(info.duration, 0);
        segment.fps = num(info.fps, 0);
      }
    }
    if (pendingVideo.length) {
      this.sync();
      this.renderDialog();
      this.setStatus(`已读取 ${pendingVideo.length} 段视频信息`);
    }
  }

  closeDialog() {
    if (this.dialog?.open) this.dialog.close();
    if (ACTIVE_DIALOG.ui === this) ACTIVE_DIALOG.ui = null;
  }

  findSegment(target) {
    const card = target.closest?.("[data-segment-id]");
    if (!card) return null;
    const index = this.state.segments.findIndex((item) => item.id === card.dataset.segmentId);
    return index < 0 ? null : { index, segment: this.state.segments[index] };
  }

  slotIndex(target) {
    const raw = target.dataset.index;
    return raw === undefined ? -1 : Number(raw);
  }

  onClick(event) {
    const actionEl = event.target.closest?.("[data-action]");
    if (!actionEl) return;
    const action = actionEl.dataset.action;
    if (action === "close") return this.dialog.close();
    if (action === "add-segment") return this.addSegment();
    if (action === "batch-videos") return this.batchVideos();
    if (action === "batch-images") return this.batchImages();
    if (action === "clear-all") return this.clearAll();
    if (action === "toggle-opts") return this.toggleOpts(actionEl);

    const found = this.findSegment(actionEl);
    if (!found) return;
    const { index, segment } = found;
    const slot = this.slotIndex(actionEl);

    switch (action) {
      case "video": this.pickVideo(segment); break;
      case "clear-video":
        segment.video = ""; segment.videoName = ""; segment.duration = 0; segment.fps = 0;
        this.sync(); this.renderDialog();
        break;
      case "use-detected":
        if (segment.duration > 0) segment.seconds = Number(segment.duration.toFixed(2));
        this.sync(); this.renderDialog();
        break;
      case "image-cell":
        this.pickImage(segment, slot);
        break;
      case "clear-image":
        if (slot >= 0) segment.images.splice(slot, 1);
        this.sync(); this.renderDialog();
        break;
      case "audio":
        this.pickAudio(segment, slot);
        break;
      case "clear-audio":
        if (slot >= 0) segment.audios.splice(slot, 1);
        this.sync(); this.renderDialog();
        break;
      case "up": this.move(index, -1); break;
      case "down": this.move(index, 1); break;
      case "duplicate":
        this.state.segments.splice(index + 1, 0, {
          ...structuredClone(segment), id: uid(),
        });
        this.sync(); this.renderDialog();
        break;
      case "delete":
        this.state.segments.splice(index, 1);
        this.sync(); this.renderDialog();
        break;
      default: break;
    }
  }

  toggleOpts(button) {
    const card = button.closest("[data-segment-id]");
    const box = card?.querySelector("[data-role='video-opts']");
    if (!box) return;
    const open = box.hasAttribute("hidden");
    if (open) box.removeAttribute("hidden"); else box.setAttribute("hidden", "");
    button.textContent = open ? "取帧设置 ▾" : "取帧设置 ▸";
  }

  onInput(event) {
    const target = event.target;
    if (!target.matches?.("textarea")) return;
    const found = this.findSegment(target);
    if (!found) return;
    if (target.dataset.role === "prompt") found.segment.prompt = target.value;
    this.sync();
  }

  async onChange(event) {
    const target = event.target;
    if (!target.matches?.("[data-role]")) return;

    const found = this.findSegment(target);
    if (!found) return;
    const { segment } = found;
    const slot = this.slotIndex(target);
    const role = target.dataset.role;

    if (role === "video-select") {
      const value = String(target.value || "");
      segment.video = value;
      segment.videoName = value ? value.split("/").pop() : "";
      segment.duration = 0;
      segment.fps = 0;
      this.sync();
      this.renderDialog();
      if (!value) {
        this.setStatus("已清除参考视频");
        return;
      }
      this.setStatus(`正在探测视频：${segment.videoName}`);
      const info = await probe("video", value);
      if (info) {
        segment.duration = num(info.duration, 0);
        segment.fps = num(info.fps, 0);
        if (!(segment.seconds > 0)) segment.seconds = Number(segment.duration.toFixed(2));
        this.setStatus(`${segment.videoName}：${fmtSeconds(segment.duration)} · ${segment.fps.toFixed(2)} fps`);
      } else {
        this.setStatus(`${segment.videoName} 已选择（无法读取信息）`);
      }
      this.sync();
      this.renderDialog();
      return;
    }

    if (role === "image-select" && slot >= 0) {
      const value = String(target.value || "");
      if (value) segment.images[slot] = { file: value, name: value.split("/").pop() };
      else segment.images.splice(slot, 1);
      this.sync();
      this.renderDialog();
      return;
    }

    if (role === "audio-select" && slot >= 0) {
      const value = String(target.value || "");
      if (value) segment.audios[slot] = { file: value, name: value.split("/").pop() };
      else segment.audios.splice(slot, 1);
      this.sync();
      this.renderDialog();
      return;
    }

    if (role === "seconds") {
      segment.seconds = Math.max(0, num(target.value, 0));
      this.sync();
      return;
    }

    if (role?.startsWith("opt-")) {
      const key = role.slice(4);
      if (key === "format") segment.videoOpts.format = String(target.value || "H3");
      else if (key === "use_audio") segment.videoOpts.use_audio = !!target.checked;
      else if (key === "select_every_nth") segment.videoOpts[key] = Math.max(1, num(target.value, 1));
      else segment.videoOpts[key] = Math.max(0, num(target.value, 0));
      this.sync();
      return;
    }
  }

  /* ---------------------- 段位增删移 ---------------------- */

  addSegment() {
    if (this.state.segments.length >= MAX_SLOTS) return this.setStatus(`最多 ${MAX_SLOTS} 段`);
    this.state.segments.push(newSegment());
    this.sync();
    this.renderDialog();
  }

  move(index, direction) {
    const target = index + direction;
    if (target < 0 || target >= this.state.segments.length) return;
    const [item] = this.state.segments.splice(index, 1);
    this.state.segments.splice(target, 0, item);
    this.sync();
    this.renderDialog();
  }

  clearAll() {
    this.state.segments = [];
    this.sync();
    this.renderDialog();
    this.setStatus("已清空所有段位");
  }

  /* ---------------------- 素材选择 / 上传 ---------------------- */

  async pickVideo(segment) {
    const files = await chooseFiles("video/*,.mp4,.mov,.mkv,.webm,.avi,.m4v,.flv,.wmv,.gif");
    if (!files.length) return;
    const file = files[0];
    try {
      this.setStatus(`正在上传视频：${file.name}`);
      const stored = await uploadFile(file);
      segment.video = stored;
      segment.videoName = file.name;
      segment.duration = 0;
      segment.fps = 0;
      this.sync();
      this.renderDialog();
      const info = await probe("video", stored);
      if (info) {
        segment.duration = num(info.duration, 0);
        segment.fps = num(info.fps, 0);
        if (!(segment.seconds > 0)) segment.seconds = Number(segment.duration.toFixed(2));
        this.setStatus(`${file.name}：${fmtSeconds(segment.duration)} · ${segment.fps.toFixed(2)} fps`);
      } else {
        this.setStatus(`${file.name} 已上传`);
      }
      this.sync();
      this.renderDialog();
    } catch (error) {
      LOG(error);
      this.setStatus(`失败：${error.message}`);
    }
  }

  async pickImage(segment, slot) {
    const files = await chooseFiles("image/*");
    if (!files.length) return;
    const file = files[0];
    try {
      this.setStatus(`正在上传图片：${file.name}`);
      const stored = await uploadFile(file);
      const entry = { file: stored, name: file.name };
      const index = slot >= 0 && slot < MAX_IMAGES ? slot : segment.images.length;
      segment.images[index] = entry;
      segment.images = segment.images.slice(0, MAX_IMAGES);
      this.sync();
      this.renderDialog();
      this.setStatus(`参考图已更新（${segment.images.length}/${MAX_IMAGES}）`);
    } catch (error) {
      LOG(error);
      this.setStatus(`失败：${error.message}`);
    }
  }

  async pickAudio(segment, slot) {
    const files = await chooseFiles("audio/*,.wav,.mp3,.flac,.ogg,.m4a,.aac,.opus");
    if (!files.length) return;
    const file = files[0];
    try {
      this.setStatus(`正在上传音频：${file.name}`);
      const stored = await uploadFile(file);
      const entry = { file: stored, name: file.name };
      const index = slot >= 0 && slot < MAX_AUDIOS ? slot : segment.audios.length;
      segment.audios[index] = entry;
      segment.audios = segment.audios.slice(0, MAX_AUDIOS);
      this.sync();
      this.renderDialog();
      const info = await probe("audio", stored);
      this.setStatus(info
        ? `${file.name}：${fmtSeconds(info.duration)} · ${info.sample_rate} Hz`
        : `${file.name} 已上传`);
    } catch (error) {
      LOG(error);
      this.setStatus(`失败：${error.message}`);
    }
  }

  async batchVideos() {
    if (this.busy) return;
    const files = await chooseFiles("video/*,.mp4,.mov,.mkv,.webm,.avi,.m4v,.flv,.wmv,.gif", true);
    if (!files.length) return;
    this.busy = true;
    try {
      let done = 0;
      for (const file of files) {
        if (this.state.segments.length >= MAX_SLOTS) {
          this.setStatus(`已达 ${MAX_SLOTS} 段上限，剩余文件未导入`);
          break;
        }
        this.setStatus(`正在上传视频 ${done + 1}/${files.length}：${file.name}`);
        const stored = await uploadFile(file);
        const info = await probe("video", stored);
        const duration = num(info?.duration, 0);
        this.state.segments.push(newSegment({
          video: stored,
          videoName: file.name,
          duration,
          fps: num(info?.fps, 0),
          seconds: Number(duration.toFixed(2)),
        }));
        done += 1;
        this.sync();
        this.renderDialog();
      }
      this.setStatus(`已导入 ${done} 段视频`);
    } catch (error) {
      LOG(error);
      this.setStatus(`失败：${error.message}`);
    } finally {
      this.busy = false;
    }
  }

  async batchImages() {
    if (this.busy) return;
    const files = await chooseFiles("image/*", true);
    if (!files.length) return;
    this.busy = true;
    try {
      let cursor = this.state.segments.length ? 0 : -1;
      let done = 0;
      for (const file of files) {
        if (!this.state.segments.length) {
          this.state.segments.push(newSegment());
          cursor = 0;
        }
        // 依次找还没满 9 张的段，从 cursor 开始轮转
        let target = null;
        for (let step = 0; step < this.state.segments.length; step++) {
          const candidate = this.state.segments[(cursor + step) % this.state.segments.length];
          if (candidate.images.length < MAX_IMAGES) { target = candidate; cursor = (cursor + step) % this.state.segments.length; break; }
        }
        if (!target) {
          if (this.state.segments.length >= MAX_SLOTS) {
            this.setStatus(`所有段位的参考图都满了，剩余文件未导入`);
            break;
          }
          target = newSegment();
          this.state.segments.push(target);
          cursor = this.state.segments.length - 1;
        }
        this.setStatus(`正在上传图片 ${done + 1}/${files.length}：${file.name}`);
        target.images.push({ file: await uploadFile(file), name: file.name });
        done += 1;
        this.sync();
        this.renderDialog();
      }
      this.setStatus(`已导入 ${done} 张参考图`);
    } catch (error) {
      LOG(error);
      this.setStatus(`失败：${error.message}`);
    } finally {
      this.busy = false;
    }
  }

  /* ---------------------- 渲染 ---------------------- */

  renderDialog() {
    const cards = this.state.segments.map((segment, index) => this.cardHTML(segment, index)).join("");
    this.dialog.innerHTML = `
      <div class="swh3-head">
        <div class="swh3-brand">SW 海螺H3 · 多图参考素材输入</div>
        <button class="swh3-button primary" data-action="add-segment">➕ 新增段位</button>
        <button class="swh3-button" data-action="batch-videos">批量上传视频</button>
        <button class="swh3-button" data-action="batch-images">批量上传图片</button>
        <button class="swh3-button danger" data-action="clear-all">清空</button>
        <div class="swh3-spacer"></div>
        <div class="swh3-status">${escapeHTML(this.status)}</div>
        <button class="swh3-button" data-action="close">完成</button>
      </div>
      <div class="swh3-content">
        ${cards
          ? `<div class="swh3-list">${cards}</div>`
          : `<div class="swh3-empty"><div><b>还没有段位</b><br>点「➕ 新增段位」或「批量上传视频」，然后每段选好<b>参考视频 / 参考图 / 参考音频</b>，填<b>提示词</b>与<b>使用时长</b>。<br>
             <span class="swh3-dim">提示词里用 &lt;Picture 1..9&gt; / &lt;Video 1&gt; / &lt;Audio 1..3&gt; 引用素材。</span></div></div>`}
      </div>`;
  }

  cardHTML(segment, index) {
    const missing = [];
    if (!segment.video && !segment.images.length && !segment.audios.length) missing.push("参考素材");
    if (!segment.prompt.trim()) missing.push("提示词");
    if (!(segment.seconds > 0)) missing.push("使用时长");
    const stateText = missing.length ? `缺：${missing.join(" / ")}` : "已就绪";
    const stateClass = missing.length ? "warn" : "ok";
    const opts = segment.videoOpts;

    return `
      <section class="swh3-card" data-segment-id="${escapeHTML(segment.id)}">
        <div class="swh3-card-head">
          <span class="swh3-card-title">第 ${index + 1} 段</span>
          <span class="swh3-badge ${stateClass}">${escapeHTML(stateText)}</span>
          <div class="swh3-spacer"></div>
          <button class="swh3-button" data-action="up" ${index === 0 ? "disabled" : ""}>上移</button>
          <button class="swh3-button" data-action="down" ${index === this.state.segments.length - 1 ? "disabled" : ""}>下移</button>
          <button class="swh3-button" data-action="duplicate">复制</button>
          <button class="swh3-button danger" data-action="delete">删除</button>
        </div>

        <div class="swh3-col">
          <label>参考视频（1 段）</label>
          <div class="swh3-preview">
            ${segment.video
              ? `<video src="${escapeHTML(viewURL(segment.video))}" muted preload="metadata" playsinline></video>`
              : `<div class="swh3-blank">未选择视频</div>`}
          </div>
          <div class="swh3-filename" title="${escapeHTML(segment.videoName)}">${escapeHTML(segment.videoName || "未选择")}</div>
          <select data-role="video-select">${optionsHTML(this.files.videos, segment.video)}</select>
          <div class="swh3-row">
            <button class="swh3-button" data-action="video">上传</button>
            <button class="swh3-button" data-action="clear-video" ${segment.video ? "" : "disabled"}>清除</button>
          </div>
          <div class="swh3-hint">
            检测到 ${fmtSeconds(segment.duration)}${segment.fps ? ` · ${segment.fps.toFixed(2)} fps` : ""}
            ${segment.duration > 0 ? `<button class="swh3-link" data-action="use-detected">用这个时长</button>` : ""}
          </div>
          <button class="swh3-button wide" data-action="toggle-opts">取帧设置 ▸</button>
          <div class="swh3-opts" data-role="video-opts" hidden>
            <div class="swh3-opt"><span>强制帧率</span><input type="number" data-role="opt-force_rate" min="0" max="60" step="1" value="${opts.force_rate}"></div>
            <div class="swh3-opt"><span>取帧上限</span><input type="number" data-role="opt-frame_load_cap" min="0" step="1" value="${opts.frame_load_cap}"></div>
            <div class="swh3-opt"><span>跳过片头</span><input type="number" data-role="opt-skip_first_frames" min="0" step="1" value="${opts.skip_first_frames}"></div>
            <div class="swh3-opt"><span>隔帧取样</span><input type="number" data-role="opt-select_every_nth" min="1" step="1" value="${opts.select_every_nth}"></div>
            <div class="swh3-opt"><span>宽(0=原)</span><input type="number" data-role="opt-custom_width" min="0" step="32" value="${opts.custom_width}"></div>
            <div class="swh3-opt"><span>高(0=原)</span><input type="number" data-role="opt-custom_height" min="0" step="32" value="${opts.custom_height}"></div>
            <div class="swh3-opt"><span>格式</span>
              <select data-role="opt-format">
                ${FORMATS.map((name) => `<option value="${name}"${name === opts.format ? " selected" : ""}>${name}</option>`).join("")}
              </select>
            </div>
            <label class="swh3-check"><input type="checkbox" data-role="opt-use_audio" ${opts.use_audio ? "checked" : ""}> 用视频原声当参考音频</label>
            <div class="swh3-dim">0 帧率 = 用原速；取帧上限 0 = 不限（H3 参考视频建议 2~15 秒）</div>
          </div>
        </div>

        <div class="swh3-col">
          <label>参考图（最多 ${MAX_IMAGES} 张，提示词写 &lt;Picture 1..${MAX_IMAGES}&gt;）</label>
          <div class="swh3-imggrid">
            ${Array.from({ length: MAX_IMAGES }, (_, i) => {
              const item = segment.images[i];
              return `<div class="swh3-imgcell">
                <div class="swh3-thumb" data-action="image-cell" data-index="${i}" title="${item ? escapeHTML(item.name) : "点击上传第 " + (i + 1) + " 张"}">
                  ${item ? `<img src="${escapeHTML(viewURL(item.file))}" alt="">` : `<span>+</span>`}
                </div>
                <select data-role="image-select" data-index="${i}">${optionsHTML(this.files.images, item?.file)}</select>
                <button class="swh3-mini" data-action="clear-image" data-index="${i}" ${item ? "" : "disabled"}>×</button>
              </div>`;
            }).join("")}
          </div>
          <div class="swh3-dim">已用 ${segment.images.length}/${MAX_IMAGES} 张 · 点「+」上传，或从下拉里选已有图</div>
        </div>

        <div class="swh3-col narrow">
          <label>参考音频（最多 ${MAX_AUDIOS} 条）</label>
          ${Array.from({ length: MAX_AUDIOS }, (_, i) => {
            const item = segment.audios[i];
            return `<div class="swh3-audiorow">
              <div class="swh3-audioname" title="${escapeHTML(item?.name || "")}">${escapeHTML(item?.name || `音频 ${i + 1}`)}</div>
              <select data-role="audio-select" data-index="${i}">${optionsHTML(this.files.audios, item?.file)}</select>
              <div class="swh3-row">
                <button class="swh3-button" data-action="audio" data-index="${i}">上传</button>
                <button class="swh3-button" data-action="clear-audio" data-index="${i}" ${item ? "" : "disabled"}>清除</button>
              </div>
            </div>`;
          }).join("")}
          <div class="swh3-dim">提示词里用 &lt;Audio 1..${MAX_AUDIOS}&gt; 引用</div>
        </div>

        <div class="swh3-col text">
          <label>提示词</label>
          <textarea data-role="prompt" placeholder="例：&lt;Picture 1&gt; 里的女孩在 &lt;Video 1&gt; 的动作下……">${escapeHTML(segment.prompt)}</textarea>
          <div class="swh3-seconds-row">
            <span>使用时长</span>
            <input class="swh3-seconds" data-role="seconds" type="number" min="0" max="60" step="0.1" value="${segment.seconds}">
            <span>秒</span>
          </div>
          <div class="swh3-dim">填 0 = 有参考视频就跟视频长度，否则用一体节点的「默认时长」。会自动吸附到 H3 帧网格 5+17n（24fps）。</div>
        </div>
      </section>`;
  }

  reload() {
    this.state = normaliseState(this.read());
    this.renderNode();
    if (this.dialog?.open) this.renderDialog();
  }

  destroy() {
    this.closeDialog();
    if (this.dialog) {
      this.dialog.remove();
      this.dialog = null;
    }
  }
}

/* ------------------------------ 样式 ------------------------------ */

function installStyles() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = `
    .swh3-node { box-sizing:border-box; width:100%; padding:9px; color:#172033; background:#f7f9fc;
      border:1px solid #cbd5e1; border-radius:8px; font:12px/1.45 "Segoe UI",Arial,sans-serif; }
    .swh3-node *,.swh3-modal * { box-sizing:border-box; }
    .swh3-node-title { font-weight:700; color:#12233f; margin-bottom:4px; }
    .swh3-node-summary { color:#526176; min-height:18px; margin-bottom:7px; }
    .swh3-node-hint { margin-top:6px; color:#7b8798; font-size:11px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .swh3-button { min-height:28px; padding:4px 9px; border:1px solid #b8c4d5; border-radius:6px;
      color:#20304a; background:#fff; cursor:pointer; font:12px "Segoe UI",Arial,sans-serif; }
    .swh3-button:hover { border-color:#3975d5; background:#f2f7ff; }
    .swh3-button:disabled { opacity:.45; cursor:not-allowed; }
    .swh3-button.primary { color:#fff; border-color:#275fb5; background:#2f6fcc; }
    .swh3-button.danger { color:#a72535; }
    .swh3-button.wide { width:100%; text-align:left; }
    .swh3-mini { width:20px; min-height:20px; padding:0; line-height:1; border:1px solid #c9d4e3;
      border-radius:4px; background:#fff; color:#8a4b0c; cursor:pointer; }
    .swh3-mini:disabled { opacity:.3; cursor:not-allowed; }
    .swh3-link { margin-left:6px; padding:0; border:0; background:none; color:#2f6fcc;
      cursor:pointer; text-decoration:underline; font:12px "Segoe UI",Arial,sans-serif; }
    .swh3-modal { width:min(1560px,97vw); height:min(900px,95vh); padding:0; border:1px solid #aebbd0;
      border-radius:12px; color:#172033; background:#f4f7fb; box-shadow:0 24px 90px #17203355;
      font:13px/1.45 "Segoe UI",Arial,sans-serif; }
    .swh3-modal::backdrop { background:#17203366; backdrop-filter:blur(2px); }
    .swh3-head { position:sticky; top:0; z-index:4; display:flex; align-items:center; gap:8px; padding:11px 14px;
      border-bottom:1px solid #cbd5e1; background:#fff; }
    .swh3-brand { font-size:17px; font-weight:750; color:#15345e; }
    .swh3-spacer { flex:1; }
    .swh3-status { color:#5c6a80; max-width:300px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .swh3-check { display:inline-flex; align-items:center; gap:5px; color:#4b5c73; }
    .swh3-content { height:calc(100% - 56px); overflow:auto; padding:14px; }
    .swh3-empty { display:grid; place-items:center; min-height:320px; border:2px dashed #c5cfdd; border-radius:10px;
      color:#64748b; background:#fff; text-align:center; }
    .swh3-dim { color:#8794a8; font-size:11px; }
    .swh3-list { display:grid; gap:12px; }
    /* align-items:start 很关键：不然某一列（如展开「取帧设置」后的视频列）变高，
       其它列会被 flex 压缩，参考图 3×3 的方格子就会被压扁。 */
    .swh3-card { display:grid; align-items:start;
      grid-template-columns:206px minmax(300px,1fr) 224px minmax(280px,340px); gap:12px;
      padding:12px; border:1px solid #cbd5e1; border-radius:10px; background:#fff; box-shadow:0 2px 10px #1720330d; }
    .swh3-card-head { grid-column:1/-1; display:flex; align-items:center; gap:8px; padding-bottom:8px; border-bottom:1px solid #e2e8f0; }
    .swh3-card-title { font-weight:700; color:#16355e; }
    .swh3-badge { padding:2px 8px; border-radius:10px; font-size:12px; font-weight:650; }
    .swh3-badge.ok { color:#12633f; background:#dcfce7; }
    .swh3-badge.warn { color:#8a4b0c; background:#fef3c7; }
    .swh3-col { display:flex; flex-direction:column; gap:6px; min-width:0; }
    .swh3-col.narrow { gap:8px; }
    .swh3-col label { display:block; color:#4b5c73; font-weight:650; }
    .swh3-preview video,.swh3-preview img { width:100%; height:120px; object-fit:cover;
      border:1px solid #cad3e0; border-radius:7px; background:#0d1420; }
    .swh3-blank { display:grid; place-items:center; width:100%; height:120px; color:#94a3b8;
      border:1px dashed #cbd5e1; border-radius:7px; background:#f8fafc; font-size:12px; }
    .swh3-filename { color:#64748b; font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .swh3-col select { width:100%; padding:5px; border:1px solid #b9c6d8; border-radius:6px; background:#fff; }
    .swh3-row { display:flex; gap:6px; }
    .swh3-hint { color:#708198; font-size:12px; }
    /* ⚠ 必须显式写 [hidden]：class 上的 display:grid 会盖掉 hidden 属性的默认 display:none，
       否则「取帧设置」看着是折叠的、其实一直展开着。 */
    .swh3-opts { display:grid; grid-template-columns:1fr 1fr; gap:6px; padding:8px; border:1px dashed #cbd5e1;
      border-radius:7px; background:#f8fafc; }
    .swh3-opts[hidden] { display:none; }
    .swh3-opts .swh3-check,.swh3-opts .swh3-dim { grid-column:1/-1; }
    .swh3-opt { display:flex; flex-direction:column; gap:2px; color:#54637a; font-size:11px; }
    .swh3-opt input,.swh3-opt select { padding:4px; border:1px solid #b9c6d8; border-radius:5px; font-size:12px; }
    .swh3-imggrid { display:grid; grid-template-columns:repeat(3,1fr); gap:6px; }
    .swh3-imgcell { display:flex; flex-direction:column; gap:3px; min-width:0; }
    .swh3-thumb { position:relative; display:grid; place-items:center; aspect-ratio:1/1; overflow:hidden;
      border:1px dashed #c3cede; border-radius:6px; background:#f8fafc; color:#9aa7b8; font-size:20px; cursor:pointer; }
    .swh3-thumb:hover { border-color:#3975d5; background:#f2f7ff; }
    .swh3-thumb img { width:100%; height:100%; object-fit:cover; }
    .swh3-imgcell select { font-size:11px; padding:3px; }
    .swh3-imgcell .swh3-mini { align-self:flex-end; }
    .swh3-audiorow { display:flex; flex-direction:column; gap:4px; padding:7px; border:1px solid #e2e8f0; border-radius:7px; }
    .swh3-audioname { color:#64748b; font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .swh3-col.text textarea { width:100%; min-height:150px; resize:vertical; padding:8px;
      border:1px solid #b9c6d8; border-radius:7px; outline:none; font:13px/1.5 "Segoe UI",Arial,sans-serif; }
    .swh3-col.text textarea:focus,.swh3-seconds:focus,.swh3-opt input:focus { border-color:#3975d5; box-shadow:0 0 0 2px #3975d522; }
    .swh3-seconds-row { display:flex; flex-wrap:nowrap; white-space:nowrap; align-items:center; gap:7px; color:#5c6a80; }
    .swh3-seconds { width:104px; flex:0 0 auto; padding:6px; border:1px solid #b9c6d8; border-radius:6px; }
    @media (max-width:1200px) { .swh3-card { grid-template-columns:1fr 1fr; } .swh3-card-head { grid-column:1/-1; } }
  `;
  document.head.appendChild(style);
}

/* ------------------------------ 注册 ------------------------------ */

app.registerExtension({
  name: "SW.H3MultiRef.Segments",

  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== NODE_NAME) return;
    installStyles();
    LOG("注册节点钩子:", NODE_NAME);

    const originalCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = originalCreated?.apply(this, arguments);
      const node = this;
      const dataWidget = node.widgets?.find((widget) => widget.name === DATA_WIDGET);
      if (!dataWidget) return result;

      // 1) 隐藏承载 JSON 的原生文本框
      dataWidget.type = "hidden";
      dataWidget.computeSize = () => [0, -4];
      const hideNativeWidget = () => {
        const element = dataWidget.element || dataWidget.inputEl;
        if (!element) return;
        element.style.setProperty("display", "none", "important");
        element.style.setProperty("pointer-events", "none", "important");
        const host = element.closest?.(".dom-widget") || element.parentElement;
        if (host) {
          host.style.setProperty("display", "none", "important");
          host.style.setProperty("height", "0px", "important");
          host.style.setProperty("min-height", "0px", "important");
        }
      };
      hideNativeWidget();
      requestAnimationFrame(hideNativeWidget);

      // 2) 挂上 DOM 面板（小节点摘要 + 打开按钮）
      const root = document.createElement("div");
      const panel = node.addDOMWidget(PANEL_WIDGET, "div", root, {
        serialize: false,
        hideOnZoom: false,
      });
      // ⚠ addDOMWidget 的第 4 个参数只写进 options，必须再显式设一次 widget.serialize，
      //   否则 widgets_values 里会多一个 null（见 README「踩坑记录」）。
      panel.serialize = false;
      if (!panel.options) panel.options = {};
      panel.options.serialize = false;
      panel.computeSize = (width) => [Math.max(260, (node.size?.[0] || width || 380) - 18), 132];
      node.size = [Math.max(node.size?.[0] || 0, 380), Math.max(node.size?.[1] || 0, 236)];

      node.__swH3RefUI?.destroy?.();
      node.__swH3RefUI = new SWH3MultiRefUI(node, dataWidget, root);
      return result;
    };

    const originalConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      const result = originalConfigure?.apply(this, arguments);
      const node = this;
      // configure 阶段数据还没恢复，放到下一帧再刷新
      requestAnimationFrame(() => node.__swH3RefUI?.reload());
      return result;
    };

    const originalRemoved = nodeType.prototype.onRemoved;
    nodeType.prototype.onRemoved = function () {
      this.__swH3RefUI?.destroy();
      this.__swH3RefUI = null;
      return originalRemoved?.apply(this, arguments);
    };
  },
});
