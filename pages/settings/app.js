// 小深音乐台 · 控制台前端
// 所有请求都走 window.AstrBotPluginPage（面板自动注入的桥），
// 由面板转发到插件注册的 /astrbot_plugin_audio_to_voice/console/* 。

const bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);
const TEXT = (v) => (v === null || v === undefined || v === "" ? "—" : String(v));
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const mmss = (s) => (s ? `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}` : "—");

let page = 0;
let libLoaded = false;
let current = "state";

const CFG_FIELDS = [
  { key: "library_dirs", label: "曲库目录（分号分隔）", type: "text" },
  { key: "max_results", label: "一次最多列几首", type: "number" },
  { key: "page_size", label: "每页几首", type: "number" },
  { key: "max_segments", label: "语音最多切几段", type: "number" },
  { key: "voice_max_seconds", label: "单条语音上限秒（0=整首）", type: "number" },
  { key: "title_chars", label: "歌名截断字数", type: "number" },
  { key: "audio_bitrate", label: "转音频码率", type: "text" },
  { key: "codec", label: "语音编码 wav/silk", type: "text" },
  { key: "mode", label: "发送方式 auto/voice/file", type: "text" },
  { key: "platform_mode", label: "平台模式 auto/official/napcat", type: "text" },
  { key: "cooldown", label: "点歌冷却秒", type: "number" },
  { key: "session_ttl", label: "歌单有效期秒", type: "number" },
  { key: "show_covers", label: "歌单配封面", type: "bool" },
  { key: "show_quality", label: "歌单标音质", type: "bool" },
  { key: "save_downloaded", label: "点歌顺手入库", type: "bool" },
  { key: "reply_hint", label: "发送前加说明", type: "bool" },
  { key: "bili_music_only", label: "B站只搜音乐区", type: "bool" },
];

function renderPills(state) {
  const login = state.login || {};
  $("pills").innerHTML = [
    login.logged_in
      ? `<span class="pill ok">已登录 · ${esc(TEXT(login.uname))} · ${esc(TEXT(login.vip))}</span>`
      : `<span class="pill warn">未登录 · 只能搜歌/普通音轨</span>`,
    `<span class="pill">曲库 ${state.library?.count ?? 0} 首 · ${state.library?.total_mb ?? 0}MB</span>`,
    `<span class="pill">临时 ${state.temp?.mb ?? 0}MB / ${state.temp?.files ?? 0} 个</span>`,
    `<span class="pill">语音上限 ${state.limits?.official_max_seconds ?? 300}s</span>`,
  ].join("");
}

function renderState(state) {
  $("state-grid").innerHTML = [
    ["B 站登录", state.login?.logged_in ? "是" : "否"],
    ["账号", state.login?.uname || "—"],
    ["会员", state.login?.vip || "—"],
    ["可无损", state.login?.can_flac ? "是" : "否"],
    ["曲库首数", state.library?.count],
    ["曲库体积", `${state.library?.total_mb ?? 0} MB`],
    ["临时文件", `${state.temp?.mb ?? 0} MB`],
    ["语音上限", `${state.limits?.official_max_seconds ?? 300}s`],
    ["切片余量", `${state.limits?.slice ?? 299.5}s`],
    ["会话中", state.runtime?.sessions ?? 0],
    ["冷却中", state.runtime?.cooldowns ?? 0],
  ]
    .map(([k, v]) => `<div class="kv"><span>${k}</span><span>${esc(TEXT(v))}</span></div>`)
    .join("");
  $("state-paths").textContent =
    `曲库：${(state.library?.dirs || []).join(" ｜ ") || "未配置"}　临时：${TEXT(state.temp?.dir)}`;
}

function renderConfig(cfg) {
  $("cfg-grid").innerHTML = CFG_FIELDS.map((f) => {
    const v = cfg?.[f.key];
    let input;
    if (f.type === "bool") {
      input = `<input type="checkbox" data-key="${f.key}" ${v ? "checked" : ""} />`;
    } else if (f.type === "number") {
      input = `<input type="number" data-key="${f.key}" value="${esc(TEXT(v))}" style="width:100%" />`;
    } else {
      input = `<input type="text" data-key="${f.key}" value="${esc(TEXT(v))}" style="width:100%" />`;
    }
    return `<div class="kv" style="flex-direction:column; align-items:stretch">
      <span>${f.label}</span>${input}</div>`;
  }).join("");
}

async function loadState() {
  const state = await bridge.apiGet("console/state");
  renderPills(state);
  renderState(state);
  renderConfig(state.config || {});
  return state;
}

async function loadLibrary(reset = false) {
  if (reset) page = 0;
  const q = $("q").value.trim();
  const probe = $("probe").checked ? "1" : "0";
  $("lib-body").innerHTML = `<tr><td colspan="4" class="dim">加载中…</td></tr>`;
  const data = await bridge.apiGet("console/library", { q, page, size: 50, probe });
  libLoaded = true;
  const rows = data.items || [];
  $("lib-body").innerHTML = rows.length
    ? rows.map((r) => `<tr>
        <td>${esc(r.name)}</td>
        <td class="num">${r.seconds !== undefined ? mmss(r.seconds) : "—"}</td>
        <td class="num">${r.mb}MB</td>
        <td class="num">${esc(r.mtime)}</td></tr>`).join("")
    : `<tr><td colspan="4" class="dim">没有匹配的文件</td></tr>`;
  const from = rows.length ? page * data.size + 1 : 0;
  $("lib-info").textContent = `${from}-${page * data.size + rows.length} / 共 ${data.total} 首` +
    (probe === "1" ? "（时长最多量 60 首）" : "");
}

function say(text) {
  const out = $("maint-out");
  out.hidden = false;
  out.textContent = text;
}

function showTab(name) {
  current = name;
  document.querySelectorAll("#tabs button").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === name);
  });
  document.querySelectorAll("section[id^='tab-']").forEach((s) => {
    s.hidden = s.id !== `tab-${name}`;
  });
  if (name === "library" && !libLoaded) {
    loadLibrary().catch((e) => say(`曲库加载失败：${e.message}`));
  }
}

function bind() {
  document.querySelectorAll("#tabs button").forEach((b) => {
    b.onclick = () => showTab(b.dataset.tab);
  });

  $("refresh").onclick = async () => {
    try {
      await loadState();
      if (current === "library") await loadLibrary();
    } catch (e) { say(`刷新失败：${e.message}`); }
  };

  $("search").onclick = () => loadLibrary(true).catch((e) => say(`查询失败：${e.message}`));
  $("q").addEventListener("keydown", (e) => { if (e.key === "Enter") loadLibrary(true); });
  $("probe").onchange = () => loadLibrary(true);
  $("prev").onclick = () => { if (page > 0) { page -= 1; loadLibrary(); } };
  $("next").onclick = () => { page += 1; loadLibrary(); };

  $("save").onclick = async () => {
    const payload = {};
    document.querySelectorAll("#cfg-grid input[data-key]").forEach((el) => {
      const field = CFG_FIELDS.find((f) => f.key === el.dataset.key);
      if (!field) return;
      if (field.type === "bool") payload[el.dataset.key] = el.checked;
      else if (field.type === "number") payload[el.dataset.key] = Number(el.value);
      else payload[el.dataset.key] = el.value;
    });
    try {
      const res = await bridge.apiPost("console/save", payload);
      say(res.ok ? `已保存：${Object.keys(res.changed || {}).join("、") || "没有改动"}` : "保存失败");
      await loadState();
    } catch (e) { say(`保存失败：${e.message}`); }
  };

  $("clean").onclick = async () => {
    say("清理中…");
    try {
      const res = await bridge.apiPost("console/cleanup", {});
      say((res.report || []).join("\n") || "清理完成（没有输出）");
      await loadState();
    } catch (e) { say(`清理失败：${e.message}`); }
  };

  $("logs").onclick = async () => {
    say("读日志…");
    try {
      const res = await bridge.apiGet("console/log", { lines: 80 });
      say((res.lines || []).join("\n") || "日志是空的");
    } catch (e) { say(`读日志失败：${e.message}`); }
  };
}

(async function main() {
  if (!bridge || typeof bridge.apiGet !== "function") {
    $("bridge-note").hidden = false;
    ["refresh", "search", "prev", "next", "save", "clean", "logs"].forEach((id) => {
      const el = $(id);
      if (el) el.disabled = true;
    });
    $("lib-body").innerHTML = `<tr><td colspan="4" class="dim">页面桥不可用，请用聊天命令</td></tr>`;
    return;
  }
  document.body.classList.remove("bridge-off");
  bind();
  showTab("state");
  try {
    await loadState();
  } catch (e) {
    say(`加载失败：${e.message}`);
  }
})();
