/* WorkBuddy 一体化控制台 —— 前端逻辑。
   零依赖、零构建：直接由后端当静态文件托管。
   刻意不用任何框架或打包器 —— armv7 设备上少一层构建就少一类坑。 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  me: null,
  view: "dashboard",
  refreshTimer: null,
  accounts: null,
  settings: null,
};

/* ── 基础工具 ──────────────────────────────────────────── */
function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "err" ? 7000 : 3600);
}

async function api(path, { method = "GET", body, silent = false } = {}) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    credentials: "same-origin",
  });
  let data = null;
  const text = await res.text();
  if (text) {
    try { data = JSON.parse(text); } catch { data = { detail: text.slice(0, 300) }; }
  }
  if (!res.ok) {
    const msg = data?.detail || data?.error?.message || data?.message || `HTTP ${res.status}`;
    if (res.status === 401) { showLogin(); }
    if (!silent) throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    return data;
  }
  return data;
}

function fmtTime(ts) {
  if (!ts) return "—";
  const d = ts > 1e12 ? new Date(ts) : new Date(ts * 1000);
  if (isNaN(d.getTime())) return "—";
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function fmtDur(sec) {
  if (!sec || sec < 0) return "—";
  if (sec < 60) return `${Math.round(sec)} 秒`;
  if (sec < 3600) return `${Math.floor(sec / 60)} 分 ${Math.round(sec % 60)} 秒`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} 小时 ${Math.floor((sec % 3600) / 60)} 分`;
  return `${Math.floor(sec / 86400)} 天 ${Math.floor((sec % 86400) / 3600)} 小时`;
}

function fmtNum(n) {
  if (n === null || n === undefined) return "—";
  return new Intl.NumberFormat("zh-CN").format(n);
}

/* ── 登录 ──────────────────────────────────────────────── */
function showLogin() {
  $("#login-mask").classList.remove("hidden");
  $("#app").classList.add("hidden");
}

function showApp() {
  $("#login-mask").classList.add("hidden");
  $("#app").classList.remove("hidden");
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const errBox = $("#login-error");
  errBox.classList.add("hidden");
  try {
    await api("/api/login", {
      method: "POST",
      body: { username: fd.get("username"), password: fd.get("password") },
    });
    await boot();
  } catch (err) {
    errBox.textContent = err.message;
    errBox.classList.remove("hidden");
  }
});

$("#btn-logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST", silent: true });
  state.me = null;
  showLogin();
});

/* ── 路由 ──────────────────────────────────────────────── */
const TITLES = {
  dashboard: "仪表盘", accounts: "账号管理", keys: "API 密钥",
  logs: "请求日志", usage: "用量统计", models: "模型中心",
  playground: "聊天测试台", settings: "系统设置",
};

const VIEWS = {};

// 当前视图的清理函数（切视图时调用）。目前只有账号页的加号轮询用得上。
let viewCleanup = null;

function currentView() {
  const hash = (location.hash || "#/dashboard").replace(/^#\//, "").split("?")[0];
  return TITLES[hash] ? hash : "dashboard";
}

async function route() {
  // 切视图前先清掉上一个视图留下的定时器，否则加号的轮询会跟着用户到处跑。
  if (viewCleanup) {
    try { viewCleanup(); } catch { /* 清理失败不该挡住导航 */ }
    viewCleanup = null;
  }
  state.view = currentView();
  $("#view-title").textContent = TITLES[state.view];
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === state.view));
  const host = $("#view");
  host.innerHTML = '<div class="empty">加载中…</div>';
  try {
    await VIEWS[state.view](host);
  } catch (err) {
    host.innerHTML = `<div class="card"><h3>加载失败</h3><p class="error">${esc(err.message)}</p></div>`;
  }
  renderUpstreamPill();
}

window.addEventListener("hashchange", route);
$("#btn-refresh").addEventListener("click", route);

/* ── 上游状态药丸 ──────────────────────────────────────── */
async function renderUpstreamPill() {
  const pill = $("#upstream-pill");
  try {
    const d = await api("/api/system/health", { silent: true });
    if (!d) return;
    const h = d.upstream;
    const cls = h.health === "ok" ? "ok" : h.health === "degraded" ? "warn" : "bad";
    const label = h.health === "ok" ? "上游正常" : h.health === "degraded" ? "上游异常" : "上游未运行";
    pill.innerHTML = `<span class="dot ${cls}"></span>${label}
      <span class="muted tiny">${d.accounts.healthy}/${d.accounts.total} 可用</span>`;
    $("#meta-accounts").textContent =
      `账号 ${d.accounts.healthy}/${d.accounts.total} 可用 · 冷却 ${d.accounts.cooling} · 停用 ${d.accounts.disabled}`;
  } catch {
    pill.innerHTML = '<span class="dot bad"></span>状态不可用';
  }
}

/* ══════════════════════════════════════════════════════════
   仪表盘
   ══════════════════════════════════════════════════════════ */
VIEWS.dashboard = async (host) => {
  const [health, summary, accounts, about] = await Promise.all([
    api("/api/system/health"),
    api("/api/stats/summary?hours=24"),
    api("/api/accounts", { silent: true }),
    api("/api/system/about"),
  ]);
  const up = health.upstream;
  const t = summary.totals;
  const a = health.accounts;

  const mem = about.memory_mb || {};
  const disk = about.disk || {};
  const creditsTotal = (accounts?.accounts || []).reduce((s, x) => s + (x.credits || 0), 0);

  const problem = (accounts?.accounts || []).filter(
    (x) => x.state !== "available" || x.consecutive_fails > 0
  ).slice(0, 8);

  host.innerHTML = `
    <div class="grid g4">
      <div class="stat"><div class="label">可用账号</div>
        <div class="value">${a.healthy}<span class="muted" style="font-size:14px"> / ${a.total}</span></div>
        <div class="sub">冷却 ${a.cooling} · 停用 ${a.disabled}</div></div>
      <div class="stat"><div class="label">24 小时请求</div>
        <div class="value">${fmtNum(t.requests)}</div>
        <div class="sub">成功率 ${t.success_rate}% · 失败 ${t.errors}</div></div>
      <div class="stat"><div class="label">平均延迟</div>
        <div class="value sm">${fmtNum(t.avg_latency_ms)} ms</div>
        <div class="sub">Token ${fmtNum(t.prompt_tokens + t.completion_tokens)}</div></div>
      <div class="stat"><div class="label">账号积分合计</div>
        <div class="value sm">${fmtNum(creditsTotal)}</div>
        <div class="sub">24h 消耗 ${t.credits}</div></div>
    </div>

    <div class="grid g2">
      <div class="card">
        <div class="card-head"><h3>上游网关进程</h3>
          <span class="tag ${up.running ? "ok" : "bad"}">${up.running ? "运行中" : "已停止"}</span></div>
        <div class="acc-meta">
          <span>PID <b>${up.pid ?? "—"}</b></span>
          <span>运行 <b>${fmtDur(up.uptime_sec)}</b></span>
          <span>自动重启 <b>${up.restarts}</b> 次</span>
          <span>上次退出码 <b>${up.last_exit_code ?? "—"}</b></span>
        </div>
        <div class="acc-meta" style="margin-top:6px">
          <span>提交 <code>${esc((up.commit || about.upstream_commit || "").slice(0, 10) || "未知")}</code></span>
          <span>仓库 <code>${esc(about.upstream_repo)}</code></span>
        </div>
        ${up.last_error ? `<p class="error tiny">${esc(up.last_error)}</p>` : ""}
        <div class="acc-acts" style="margin-top:12px">
          <button class="btn tiny" data-act="start" ${up.running ? "disabled" : ""}>启动</button>
          <button class="btn tiny" data-act="restart">重启</button>
          <button class="btn tiny danger" data-act="stop" ${up.running ? "" : "disabled"}>停止</button>
          <button class="btn tiny" data-nav="settings">查看日志与设置</button>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><h3>设备资源</h3><span class="muted tiny">${esc(about.machine)} · ${about.cpu_count} 核</span></div>
        <div class="acc-meta">
          <span>内存可用 <b>${mem.memavailable ?? "—"} MB</b> / 共 ${mem.memtotal ?? "—"} MB</span>
        </div>
        <div class="bar ${(mem.memavailable || 0) < 120 ? "warn" : "ok"}" style="margin:8px 0 12px">
          <span style="width:${mem.memtotal ? Math.round((1 - mem.memavailable / mem.memtotal) * 100) : 0}%"></span>
        </div>
        <div class="acc-meta">
          <span>磁盘可用 <b>${disk.free_mb ?? "—"} MB</b> / 共 ${disk.total_mb ?? "—"} MB</span>
        </div>
        <div class="bar ${(disk.free_mb || 0) < 512 ? "warn" : "ok"}" style="margin:8px 0 12px">
          <span style="width:${disk.total_mb ? Math.round((1 - disk.free_mb / disk.total_mb) * 100) : 0}%"></span>
        </div>
        <div class="acc-meta">
          <span>Python ${esc(about.python)}</span>
          <span>面板 v${esc(about.manager_version)}</span>
          <span>运行 ${fmtDur(about.uptime_sec)}</span>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h2>账号健康度</h2>
        <button class="btn tiny" data-nav="accounts">管理账号</button></div>
      ${accounts?.upstream_error
        ? `<div class="empty">上游不可用：${esc(accounts.upstream_error)}</div>`
        : problem.length === 0
          ? '<div class="empty">全部账号状态正常</div>'
          : `<div class="grid g3">${problem.map(accCard).join("")}</div>`}
    </div>

    ${accounts?.admin_enabled === false ? `
      <div class="card">
        <h3>⚠️ admin 接口未开启</h3>
        <p class="muted">上游 <code>admin.enabled</code> 当前为 false，面板里的「停用 / 启用 / 复活」会返回 409。
        到系统设置里打开即可（保存后会自动重启上游）。</p>
      </div>` : ""}
  `;

  $$("[data-act]", host).forEach((b) =>
    b.addEventListener("click", async () => {
      b.disabled = true;
      try {
        const r = await api(`/api/system/upstream/${b.dataset.act}`, { method: "POST" });
        toast(r.detail, r.ok ? "ok" : "err");
        route();
      } catch (e) { toast(e.message, "err"); b.disabled = false; }
    })
  );
  bindNav(host);
};

/* ══════════════════════════════════════════════════════════
   账号管理
   ══════════════════════════════════════════════════════════ */
const STATE_LABEL = {
  available: ["可用", "ok"], cooling: ["冷却中", "warn"],
  manual: ["手动停用", "warn"], disabled: ["已禁用", "bad"],
};

function accCard(a) {
  const [label, cls] = STATE_LABEL[a.state] || ["未知", ""];
  // 一个账号可能同时「手动停用」+「冷却中」。state 只给主导原因，
  // 这里把被折叠的另一个事实补成一枚副标签，不让任何状态被藏起来。
  const secondary = a.state !== "cooling" && a.cooling
    ? '<span class="tag warn">冷却中</span>' : "";
  const cool = a.cooling && a.cool_remaining_sec
    ? `<span>剩余 ${fmtDur(a.cool_remaining_sec)}</span>` : "";
  const reason = a.manual_reason || a.disabled_reason || a.reason || "";
  return `
    <div class="acc">
      <div class="acc-top">
        <div style="min-width:0">
          <div class="acc-name">${esc(a.nickname || a.uid || "未命名")}</div>
          <div class="acc-uid">${esc(a.uid)} · ${esc(a.realm)}</div>
        </div>
        <div style="display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-end">
          <span class="tag ${cls}">${label}</span>${secondary}
        </div>
      </div>
      <div class="acc-meta">
        <span>积分 <b>${fmtNum(a.credits)}</b></span>
        <span>成功 <b>${fmtNum(a.success_count)}</b></span>
        <span>错误 <b>${fmtNum(a.err_total)}</b></span>
        <span>连败 <b>${a.consecutive_fails}</b></span>
        ${cool}
      </div>
      ${reason ? `<div class="tiny muted" style="overflow:hidden;text-overflow:ellipsis">原因：${esc(reason)}</div>` : ""}
      <div class="acc-meta tiny">
        <span>最近成功 ${fmtTime(a.last_success)}</span>
        ${a.in_flight ? `<span>在途 ${a.in_flight}</span>` : ""}
      </div>
      <div class="acc-acts">
        ${a.manual_disabled
          ? `<button class="btn tiny" data-acc="${esc(a.uid)}" data-op="enable">恢复参与调度</button>` : ""}
        ${a.disabled
          ? `<button class="btn tiny" data-acc="${esc(a.uid)}" data-op="revive">复活账号</button>` : ""}
        ${!a.manual_disabled && !a.disabled
          ? `<button class="btn tiny danger" data-acc="${esc(a.uid)}" data-op="disable">临时停用</button>` : ""}
        <button class="btn tiny danger" data-del="${esc(a.uid)}">删除凭证</button>
      </div>
    </div>`;
}

/* ── 加号（设备授权）轮询 ──────────────────────────────────
   上游的状态端点没有「授权完成」的推送，只能问。3 秒一次是折中：
   用户在手机上手点完授权到看到结果，最多多等 3 秒，而请求量完全可以忽略。 */
const loginPoll = { timer: null, state: null };

function stopLoginPoll() {
  if (loginPoll.timer) { clearTimeout(loginPoll.timer); loginPoll.timer = null; }
}

async function loadLoginRegions(host) {
  const sel = $("#login-region", host);
  if (!sel) return;
  try {
    const d = await api("/api/accounts/login/regions", { silent: true });
    const opts = (d?.regions || []).map(
      (r) => `<option value="${esc(r.code)}">${esc(r.name)} (${esc(r.code)})</option>`);
    sel.innerHTML = opts.join("") || '<option value="">（取不到地区列表）</option>';
  } catch {
    sel.innerHTML = '<option value="">（取不到地区列表）</option>';
  }
}

function renderLoginFlow(host, r) {
  $("#login-flow", host).innerHTML = `
    <div class="acc" style="margin-top:14px">
      <div class="acc-top"><b>第 1 步 · 打开授权链接</b>
        <span class="tag info">${esc(r.realm)}${r.region ? " · " + esc(r.region) : ""}</span></div>
      <p class="muted tiny" style="margin:0">
        在浏览器或手机里打开下面的链接完成登录。国内版通常可以直接扫码。</p>
      <div class="key-box"><code>${esc(r.url)}</code>
        <button class="btn tiny" id="copy-url">复制</button>
        <a class="btn tiny" href="${esc(r.url)}" target="_blank" rel="noopener">打开</a></div>
      <div class="acc-top" style="margin-top:12px"><b>第 2 步 · 等待授权完成</b>
        <span><span class="dot warn" id="login-dot"></span>
        <span id="login-status">等待授权…</span></span></div>
      <p class="muted tiny" style="margin:0">
        面板会自动检测，授权成功后立刻落盘并收尾，不需要你回来点任何按钮。</p>
      <div class="muted tiny mono" id="login-raw" style="margin-top:4px;opacity:.72"></div>
      <div class="acc-acts">
        <button class="btn" id="btn-login-check">立即检查</button>
        <button class="btn" id="btn-login-cancel">取消</button>
      </div>
      <div id="login-result"></div>
    </div>`;

  $("#copy-url", host).addEventListener("click", () => {
    navigator.clipboard?.writeText(r.url);
    toast("链接已复制", "ok");
  });
  $("#btn-login-check", host).addEventListener("click", () => {
    stopLoginPoll();
    loginTick(host);
  });
  $("#btn-login-cancel", host).addEventListener("click", async () => {
    stopLoginPoll();
    const state = loginPoll.state;
    loginPoll.state = null;
    if (state) {
      await api("/api/accounts/login/cancel", { method: "POST", body: { state }, silent: true });
    }
    $("#login-flow", host).innerHTML = "";
    toast("已取消");
  });
}

async function loginTick(host) {
  if (!loginPoll.state) return;
  const st = $("#login-status", host);
  const dot = $("#login-dot", host);
  const raw = $("#login-raw", host);
  if (!st) return;  // 视图已被换掉，静默退出

  const d = await api(
    `/api/accounts/login/poll?state=${encodeURIComponent(loginPoll.state)}`,
    { silent: true },
  );
  if (!d) return;

  // 会话失效/超时：接口回的是 HTTP 错误体（只有 detail，没有 done 字段）。
  // 这时候继续轮询没有意义，必须明说并停下。
  if (d.detail && d.done === undefined) {
    stopLoginPoll();
    loginPoll.state = null;
    dot.className = "dot bad";
    st.textContent = "会话已失效";
    if (raw) raw.textContent = String(d.detail);
    return;
  }

  if (d.done) {
    stopLoginPoll();
    loginPoll.state = null;
    dot.className = "dot ok";
    st.textContent = "登录完成";
    if (raw) raw.textContent = "";
    renderLoginResult(host, d);
    return;
  }

  dot.className = "dot warn";
  st.textContent = `等待授权…（已查询 ${d.polls ?? 0} 次）`;
  if (raw) raw.textContent = d.message ? `上游：${d.message}` : "";
  loginPoll.timer = setTimeout(() => loginTick(host), 3000);
}

function renderLoginResult(host, d) {
  const box = $("#login-result", host);
  if (!box) return;
  box.innerHTML = `
    <div class="acc" style="margin-top:12px">
      <div class="acc-top"><b>已添加账号</b>
        <span class="tag ok">${esc(d.realm)}</span></div>
      <div class="acc-name">${esc(d.nickname || d.uid)}</div>
      <div class="acc-uid">${esc(d.uid)} · ${esc(d.file || "")} · ${d.updated ? "覆盖了原有凭证" : "新增凭证"}</div>
      ${d.message ? `<p class="muted tiny" style="margin:8px 0 0">${esc(d.message)}</p>` : ""}
      <p class="muted tiny" style="margin:8px 0 0">
        凭证已写入 <code>auths/</code>。面板正在后台重启上游把它读进账号池，
        几秒后本页会自动刷新；若下面「本地凭证文件」里仍显示「未加载」，
        点右上角「重新加载账号池」即可。</p>
    </div>`;
  toast(`已添加账号 ${d.nickname || d.uid}`, "ok");
  // 等上游重启完再刷新，否则刷出来还是「未加载」，白让人惊一下。
  setTimeout(() => { if (state.view === "accounts") route(); }, 6000);
}

VIEWS.accounts = async (host) => {
  const d = await api("/api/accounts");
  state.accounts = d;

  host.innerHTML = `
    ${d.upstream_error ? `<div class="card"><h3>上游不可用</h3><p class="error">${esc(d.upstream_error)}</p>
      <p class="muted tiny">仍可查看本地凭证文件。上游进程状态：${d.upstream_running ? "运行中" : "已停止"}</p></div>` : ""}

    <div class="card">
      <div class="card-head"><h2>添加账号</h2>
        <button class="btn tiny" id="btn-reload">重新加载账号池</button></div>
      <p class="muted tiny" style="margin:0 0 12px">
        设备授权登录：先拿一条授权链接，你在浏览器（或手机）里完成登录，
        面板每 3 秒问一次上游，<b>授权一完成这里会自动收尾</b>，不用回来点确认。
        凭证按上游格式写入 <code>auths/</code>，随后面板在后台重启上游把新号读进池。
      </p>
      <div class="row">
        <label>版本
          <select id="login-realm">
            <option value="cn">国内版（cn）</option>
            <option value="global">国际版（global）</option>
          </select>
        </label>
        <label id="login-region-wrap" class="hidden">首次注册地区
          <select id="login-region"></select>
        </label>
        <button class="btn primary" id="btn-login-start">获取授权链接</button>
      </div>
      <p class="muted tiny" id="login-region-hint" style="margin:8px 0 0;display:none">
        国际版新号需要先补注册地区才能激活 trial。不指定时自动用 Hong Kong。
      </p>
      <div id="login-flow"></div>
    </div>

    <div class="grid g4">
      <div class="stat"><div class="label">账号总数</div><div class="value">${d.total}</div>
        <div class="sub">凭证文件 ${d.files.length} 个</div></div>
      <div class="stat"><div class="label">可用</div><div class="value">${d.healthy}</div>
        <div class="sub">在途占满 ${d.in_flight_full}</div></div>
      <div class="stat"><div class="label">冷却中</div><div class="value">${d.cooling}</div>
        <div class="sub">粘性会话 ${d.sticky_sessions}</div></div>
      <div class="stat"><div class="label">已禁用</div><div class="value">${d.disabled}</div>
        <div class="sub">admin ${d.admin_enabled ? "已开启" : "未开启"}</div></div>
    </div>

    ${Object.keys(d.realm_totals || {}).length > 1 ? `
      <div class="card"><h3>按地区分布</h3>
        <div class="table-wrap"><table><thead><tr>
          <th>地区</th><th>总数</th><th>可用</th><th>冷却</th><th>禁用</th></tr></thead><tbody>
          ${Object.entries(d.realm_totals).map(([realm, c]) => `<tr>
            <td>${esc(realm)}</td><td>${c.total}</td><td>${c.healthy}</td>
            <td>${c.cooling}</td><td>${c.disabled}</td></tr>`).join("")}
        </tbody></table></div>
      </div>` : ""}

    <div class="card">
      <div class="card-head"><h2>账号列表</h2>
        <span class="muted tiny">${d.accounts.length} 个</span></div>
      ${d.accounts.length === 0
        ? '<div class="empty">还没有纳管任何账号，用上方「获取授权链接」添加第一个。</div>'
        : `<div class="grid g3">${d.accounts.map(accCard).join("")}</div>`}
    </div>

    <div class="card">
      <div class="card-head"><h2>本地凭证文件</h2>
        <span class="muted tiny">auths/ 目录</span></div>
      ${d.orphan_uids?.length
        ? `<p class="warn tiny" style="color:var(--warn)">上游内存里有 ${d.orphan_uids.length} 个账号在 auths/ 里找不到对应文件
           （${esc(d.orphan_uids.join(", "))}）——通常是文件属主不是容器运行用户，导致上游读不到。</p>` : ""}
      ${d.files.length === 0 ? '<div class="empty">auths/ 为空</div>' : `
        <div class="table-wrap"><table><thead><tr>
          <th>UID</th><th>昵称</th><th>地区</th><th>Token 到期</th><th>文件</th><th>状态</th><th></th></tr></thead><tbody>
          ${d.files.map((f) => `<tr>
            <td class="mono">${esc(f.uid)}</td>
            <td>${esc(f.nickname || "—")}</td>
            <td>${esc(f.realm || "—")}</td>
            <td>${fmtTime(f.expires_at)}</td>
            <td class="mono tiny">${esc(f.file)}</td>
            <td>${f.broken ? '<span class="tag bad">解析失败</span>'
                  : f.known ? '<span class="tag ok">已加载</span>'
                  : '<span class="tag warn">未加载</span>'}</td>
            <td>${f.realm === "global"
                  ? `<button class="btn tiny" data-region="${esc(f.uid)}">补注册地区</button>`
                  : ""}</td>
          </tr>`).join("")}
        </tbody></table></div>`}
    </div>
  `;

  $("#btn-reload", host).addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      const r = await api("/api/accounts/reload", { method: "POST" });
      toast(r.detail, r.ok ? "ok" : "err");
      route();
    } catch (err) { toast(err.message, "err"); e.target.disabled = false; }
  });

  $("#btn-login-start", host).addEventListener("click", async (e) => {
    const realm = $("#login-realm", host).value;
    const region = realm === "global" ? $("#login-region", host).value : "";
    e.target.disabled = true;
    stopLoginPoll();
    try {
      const r = await api("/api/accounts/login/start", { method: "POST", body: { realm, region } });
      renderLoginFlow(host, r);
      loginPoll.state = r.state;
      loginTick(host);
    } catch (err) {
      toast(err.message, "err");
    } finally {
      e.target.disabled = false;
    }
  });

  // 地区选择只在国际版才有意义：CN 没有地区门控。
  const realmSel = $("#login-realm", host);
  const regionWrap = $("#login-region-wrap", host);
  const regionHint = $("#login-region-hint", host);
  const syncRegion = () => {
    const isGlobal = realmSel.value === "global";
    regionWrap.classList.toggle("hidden", !isGlobal);
    regionHint.style.display = isGlobal ? "" : "none";
  };
  realmSel.addEventListener("change", syncRegion);
  syncRegion();
  loadLoginRegions(host);

  // 面板重启前发起的加号会话会从 DB 恢复：直接接着轮询，用户不必重新取链接。
  const resumed = (d.pending_logins || [])[0];
  if (resumed) {
    renderLoginFlow(host, resumed);
    loginPoll.state = resumed.state;
    const st = $("#login-status", host);
    if (st) st.textContent = "恢复上一次未完成的授权…";
    loginTick(host);
  }

  viewCleanup = stopLoginPoll;

  $$("[data-region]", host).forEach((b) =>
    b.addEventListener("click", async () => {
      const uid = b.dataset.region;
      b.disabled = true;
      b.textContent = "处理中…";
      try {
        const r = await api("/api/accounts/refresh-region", { method: "POST", body: { uid } });
        toast(r.message || "已激活", "ok");
      } catch (err) {
        toast(err.message, "err");
      } finally {
        b.disabled = false;
        b.textContent = "补注册地区";
      }
    }));

  $$("[data-acc]", host).forEach((b) =>
    b.addEventListener("click", async () => {
      const uid = b.dataset.acc;
      const op = b.dataset.op;
      let reason = "";
      if (op === "disable") {
        reason = prompt("临时停用这个账号的原因（会写进上游状态，可留空）：", "") ?? null;
        if (reason === null) return;
      }
      b.disabled = true;
      try {
        await api(`/api/accounts/${encodeURIComponent(uid)}/${op}`, {
          method: "POST", body: { reason },
        });
        toast("已提交", "ok");
        route();
      } catch (err) { toast(err.message, "err"); b.disabled = false; }
    })
  );

  $$("[data-del]", host).forEach((b) =>
    b.addEventListener("click", async () => {
      const uid = b.dataset.del;
      if (!confirm(`删除账号 ${uid} 的本地凭证文件？\n\n上游会随之重启以重新加载账号池。此操作不可撤销（需要重新扫码登录才能恢复）。`)) return;
      b.disabled = true;
      try {
        const r = await api(`/api/accounts/${encodeURIComponent(uid)}`, { method: "DELETE" });
        toast(r.detail || "已删除", r.restarted ? "ok" : "err");
        route();
      } catch (err) { toast(err.message, "err"); b.disabled = false; }
    })
  );
};

/* ══════════════════════════════════════════════════════════
   API 密钥
   ══════════════════════════════════════════════════════════ */
VIEWS.keys = async (host) => {
  const d = await api("/api/keys");
  const models = await api("/api/models", { silent: true }).catch(() => ({ models: [] }));

  host.innerHTML = `
    <div class="card">
      <div class="card-head"><h2>新建密钥</h2>
        <span class="muted tiny">明文只展示一次</span></div>
      <div class="grid g3">
        <label>名称<input id="k-name" placeholder="例如：笔记本 / 手机 / 朋友"></label>
        <label>有效期（天，留空=永久）<input id="k-days" type="number" min="1" placeholder="30"></label>
        <label>配额（积分，0=不限）<input id="k-quota" type="number" min="0" step="0.1" placeholder="0"></label>
        <label>IP 白名单（逗号分隔 CIDR，留空=不限）
          <input id="k-ip" placeholder="如 192.168.1.0/24, 10.0.0.5"></label>
        <label>模型白名单（留空=全部）
          <input id="k-models" placeholder="逗号分隔，如 deepseek-v4-flash,glm-5"></label>
      </div>
      <div class="row" style="margin-top:12px">
        <button class="btn primary" id="k-create">生成密钥</button>
      </div>
      ${models.models?.length ? `<p class="muted tiny">当前可用模型：${models.models.slice(0, 12).map((m) => esc(m.id)).join("、")}${models.models.length > 12 ? " …" : ""}</p>` : ""}
    </div>

    <div class="card">
      <div class="card-head"><h2>已分发密钥</h2><span class="muted tiny">${d.keys.length} 把</span></div>
      ${d.keys.length === 0 ? '<div class="empty">还没有密钥。生成一把就能用任何 OpenAI 兼容客户端接入了。</div>' : `
      <div class="table-wrap"><table><thead><tr>
        <th>名称</th><th>状态</th><th>配额</th><th>已用</th><th>请求</th>
        <th>最近使用</th><th>到期</th><th>限制</th><th></th></tr></thead><tbody>
        ${d.keys.map((k) => {
          const cls = k.state === "active" ? "ok" : k.state === "exhausted" ? "warn" : "bad";
          return `<tr>
            <td><b>${esc(k.name)}</b><div class="muted tiny mono">${esc(k.prefix)}…</div></td>
            <td><span class="tag ${cls}">${{
              active: "生效中", disabled: "已停用", expired: "已过期", exhausted: "配额用尽",
            }[k.state] || k.state}</span></td>
            <td>${k.quota_credits > 0 ? k.quota_credits : "不限"}
              ${k.quota_left_pct !== null ? `<div class="bar" style="width:70px;margin-top:4px">
                <span style="width:${k.quota_left_pct}%"></span></div>` : ""}</td>
            <td>${k.used_credits}</td>
            <td>${fmtNum(k.request_count)}</td>
            <td class="tiny">${fmtTime(k.last_used)}</td>
            <td class="tiny">${k.expires_at ? fmtTime(k.expires_at) : "永久"}</td>
            <td class="tiny">${k.models ? "模型：" + esc(k.models) : ""}
              ${k.ip_allow ? `<div>IP：${esc(k.ip_allow)}</div>` : ""}</td>
            <td><div class="acc-acts">
              <button class="btn tiny" data-k="${k.id}" data-op="toggle">${k.disabled ? "启用" : "停用"}</button>
              <button class="btn tiny" data-k="${k.id}" data-op="reset">清零用量</button>
              <button class="btn tiny danger" data-k="${k.id}" data-op="del">删除</button>
            </div></td>
          </tr>`;
        }).join("")}
      </tbody></table></div>`}
      <p class="muted tiny" style="margin:12px 0 0">
        接入方式：客户端把 Base URL 指向本服务的
        <code>http://&lt;设备IP&gt;:${location.port || "7864"}/v1</code>，
        API Key 填上面生成的那把。面板与网关同端口，不需要第二个端口。
      </p>
    </div>
  `;

  $("#k-create", host).addEventListener("click", async (e) => {
    const name = $("#k-name", host).value.trim();
    if (!name) return toast("请填写密钥名称", "err");
    e.target.disabled = true;
    try {
      const r = await api("/api/keys", {
        method: "POST",
        body: {
          name,
          expires_in_days: $("#k-days", host).value || null,
          quota_credits: $("#k-quota", host).value || 0,
          ip_allow: $("#k-ip", host).value.trim(),
          models: $("#k-models", host).value.split(",").map((s) => s.trim()).filter(Boolean),
        },
      });
      $("#key-plain").textContent = r.api_key;
      $("#key-mask").classList.remove("hidden");
      route();
    } catch (err) { toast(err.message, "err"); }
    finally { e.target.disabled = false; }
  });

  $$("[data-k]", host).forEach((b) =>
    b.addEventListener("click", async () => {
      const id = b.dataset.k;
      const op = b.dataset.op;
      try {
        if (op === "del") {
          if (!confirm("删除这把密钥？使用它的客户端会立刻开始收到 401。")) return;
          await api(`/api/keys/${id}`, { method: "DELETE" });
        } else if (op === "reset") {
          await api(`/api/keys/${id}`, { method: "PATCH", body: { reset_usage: true } });
        } else {
          const cur = d.keys.find((k) => String(k.id) === String(id));
          await api(`/api/keys/${id}`, { method: "PATCH", body: { disabled: !cur.disabled } });
        }
        toast("已更新", "ok");
        route();
      } catch (err) { toast(err.message, "err"); }
    })
  );
};

$("#key-copy").addEventListener("click", () => {
  navigator.clipboard?.writeText($("#key-plain").textContent);
  toast("已复制", "ok");
});
$("#key-done").addEventListener("click", () => $("#key-mask").classList.add("hidden"));

/* ══════════════════════════════════════════════════════════
   请求日志
   ══════════════════════════════════════════════════════════ */
const logPager = { offset: 0, limit: 50, status: "", model: "", q: "" };

VIEWS.logs = async (host) => {
  const qs = new URLSearchParams({
    limit: logPager.limit, offset: logPager.offset,
    status: logPager.status, model: logPager.model, q: logPager.q,
  });
  const d = await api(`/api/logs?${qs}`);

  host.innerHTML = `
    <div class="card">
      <div class="row">
        <label>状态
          <select id="f-status">
            <option value="">全部</option>
            <option value="ok" ${logPager.status === "ok" ? "selected" : ""}>成功</option>
            <option value="error" ${logPager.status === "error" ? "selected" : ""}>失败</option>
          </select></label>
        <label>模型<input id="f-model" value="${esc(logPager.model)}" placeholder="精确匹配"></label>
        <label>搜索<input id="f-q" value="${esc(logPager.q)}" placeholder="密钥名 / IP / 错误信息"></label>
        <button class="btn" id="f-apply">筛选</button>
        <button class="btn danger" id="f-purge">清空日志</button>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h2>请求记录</h2>
        <span class="muted tiny">共 ${fmtNum(d.total)} 条 · 第 ${Math.floor(logPager.offset / logPager.limit) + 1} 页</span></div>
      ${d.logs.length === 0 ? '<div class="empty">没有匹配的记录</div>' : `
      <div class="table-wrap"><table><thead><tr>
        <th>时间</th><th>密钥</th><th>来源 IP</th><th>模型</th><th>状态</th>
        <th>耗时</th><th>Token</th><th>扣费</th><th>说明</th></tr></thead><tbody>
        ${d.logs.map((l) => {
          const ok = l.status >= 200 && l.status < 400;
          return `<tr>
            <td class="tiny">${fmtTime(l.ts)}</td>
            <td>${esc(l.key_name || "—")}</td>
            <td class="mono tiny">${esc(l.ip || "—")}</td>
            <td class="mono tiny">${esc(l.model || "—")}${l.stream ? ' <span class="tag info">流</span>' : ""}</td>
            <td><span class="tag ${ok ? "ok" : "bad"}">${l.status}</span></td>
            <td class="tiny">${l.latency_ms} ms</td>
            <td class="tiny">${fmtNum((l.prompt_tokens || 0) + (l.completion_tokens || 0))}</td>
            <td class="tiny">${l.credits || 0}</td>
            <td class="tiny" style="max-width:280px;overflow:hidden;text-overflow:ellipsis"
                title="${esc(l.error)}">${esc((l.error || "").slice(0, 60))}</td>
          </tr>`;
        }).join("")}
      </tbody></table></div>
      <div class="row" style="margin-top:12px">
        <button class="btn" id="p-prev" ${logPager.offset === 0 ? "disabled" : ""}>上一页</button>
        <button class="btn" id="p-next" ${logPager.offset + logPager.limit >= d.total ? "disabled" : ""}>下一页</button>
      </div>`}
    </div>
  `;

  $("#f-apply", host).addEventListener("click", () => {
    logPager.status = $("#f-status", host).value;
    logPager.model = $("#f-model", host).value.trim();
    logPager.q = $("#f-q", host).value.trim();
    logPager.offset = 0;
    route();
  });
  $("#f-purge", host).addEventListener("click", async () => {
    if (!confirm("清空全部请求日志？统计页的数据也会一起清掉。")) return;
    await api("/api/logs", { method: "DELETE" });
    toast("已清空", "ok");
    logPager.offset = 0;
    route();
  });
  const prev = $("#p-prev", host), next = $("#p-next", host);
  prev?.addEventListener("click", () => { logPager.offset = Math.max(0, logPager.offset - logPager.limit); route(); });
  next?.addEventListener("click", () => { logPager.offset += logPager.limit; route(); });
};

/* ══════════════════════════════════════════════════════════
   用量统计
   ══════════════════════════════════════════════════════════ */
VIEWS.usage = async (host) => {
  const d = await api("/api/stats/summary?hours=168");
  const up = await api("/api/stats/upstream", { silent: true });
  const t = d.totals;
  const peak = Math.max(1, ...d.series.map((s) => s.n));

  host.innerHTML = `
    <div class="grid g4">
      <div class="stat"><div class="label">7 天请求</div><div class="value">${fmtNum(t.requests)}</div>
        <div class="sub">成功 ${fmtNum(t.ok)} · 失败 ${fmtNum(t.errors)}</div></div>
      <div class="stat"><div class="label">成功率</div><div class="value">${t.success_rate}%</div>
        <div class="sub">平均延迟 ${fmtNum(t.avg_latency_ms)} ms</div></div>
      <div class="stat"><div class="label">输入 Token</div><div class="value sm">${fmtNum(t.prompt_tokens)}</div>
        <div class="sub">输出 ${fmtNum(t.completion_tokens)}</div></div>
      <div class="stat"><div class="label">扣费合计</div><div class="value sm">${t.credits}</div>
        <div class="sub">来自上游 usage 字段</div></div>
    </div>

    <div class="card">
      <div class="card-head"><h2>请求量趋势（7 天）</h2>
        <span class="muted tiny">每 ${d.bucket_sec / 3600} 小时一格</span></div>
      ${d.series.length === 0 ? '<div class="empty">这段时间没有请求</div>' : `
        <div class="chart">
          ${d.series.map((s) => `
            <div class="col" data-tip="${fmtTime(s.t)} · ${s.n} 次 · 成功 ${s.ok}">
              <i style="height:${Math.round((s.n / peak) * 100)}%"></i>
            </div>`).join("")}
        </div>`}
    </div>

    <div class="grid g2">
      <div class="card">
        <h3>按模型</h3>
        ${d.by_model.length === 0 ? '<div class="empty">暂无数据</div>' : `
        <div class="table-wrap"><table><thead><tr>
          <th>模型</th><th>请求</th><th>Token</th><th>扣费</th><th>均延迟</th></tr></thead><tbody>
          ${d.by_model.map((m) => `<tr>
            <td class="mono tiny">${esc(m.model || "未知")}</td>
            <td>${fmtNum(m.n)}</td><td>${fmtNum(m.tokens)}</td>
            <td>${(m.credits || 0).toFixed(3)}</td><td>${Math.round(m.avg_latency || 0)} ms</td>
          </tr>`).join("")}
        </tbody></table></div>`}
      </div>
      <div class="card">
        <h3>按密钥</h3>
        ${d.by_key.length === 0 ? '<div class="empty">暂无数据</div>' : `
        <div class="table-wrap"><table><thead><tr>
          <th>密钥</th><th>请求</th><th>扣费</th></tr></thead><tbody>
          ${d.by_key.map((k) => `<tr>
            <td>${esc(k.key_name || "已删除")}</td><td>${fmtNum(k.n)}</td>
            <td>${(k.credits || 0).toFixed(3)}</td>
          </tr>`).join("")}
        </tbody></table></div>`}
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h3>上游自报指标</h3><span class="muted tiny">GET /v1/stats</span></div>
      ${up?.unavailable
        ? `<div class="empty">${esc(up.unavailable)}</div>`
        : `<pre class="pre">${esc(JSON.stringify(up, null, 2))}</pre>`}
    </div>
  `;
};

/* ══════════════════════════════════════════════════════════
   模型中心
   ══════════════════════════════════════════════════════════ */
VIEWS.models = async (host) => {
  host.innerHTML = '<div class="empty">正在从上游拉取模型列表…</div>';
  let d;
  try {
    d = await api("/api/models");
  } catch (err) {
    host.innerHTML = `<div class="card"><h3>拉取失败</h3><p class="error">${esc(err.message)}</p>
      <p class="muted tiny">模型列表由上游实时探测（缓存 1 小时）。上游没有可用账号时可能返回空列表。</p></div>`;
    return;
  }

  host.innerHTML = `
    <div class="card">
      <div class="card-head"><h2>可用模型</h2>
        <span class="muted tiny">${d.count} 个</span></div>
      <input id="m-search" placeholder="搜索模型名 / 描述 / 厂商" style="margin-bottom:12px">
      ${d.count === 0 ? '<div class="empty">上游返回空列表——通常是没有可用账号，或探测失败。</div>' : `
      <div class="table-wrap"><table><thead><tr>
        <th>模型 ID</th><th>名称</th><th>计费</th><th>厂商</th><th>标签</th></tr></thead><tbody id="m-body">
      </tbody></table></div>`}
      <p class="muted tiny" style="margin:12px 0 0">
        模型中心的数据全部来自上游实时探测，面板不维护静态名单 —— 上游改了名单这里立刻跟着变。
      </p>
    </div>
  `;

  const rows = d.models.map((m) => `
    <tr data-text="${esc([m.id, m.name, m.description, m.vendor, (m.tags || []).join(" ")].join(" ").toLowerCase())}">
      <td class="mono">${esc(m.id)}</td>
      <td>${esc(m.name || "—")}</td>
      <td class="tiny">${esc(m.credits || "—")}</td>
      <td class="tiny">${esc(m.vendor || "—")}</td>
      <td class="tiny">${(m.tags || []).map((x) => `<span class="tag">${esc(x)}</span>`).join(" ")}</td>
    </tr>`);

  const body = $("#m-body", host);
  if (body) {
    body.innerHTML = rows.join("");
    $("#m-search", host).addEventListener("input", (e) => {
      const q = e.target.value.trim().toLowerCase();
      $$("#m-body tr", host).forEach((tr) => {
        tr.style.display = !q || tr.dataset.text.includes(q) ? "" : "none";
      });
    });
  }
};

/* ══════════════════════════════════════════════════════════
   聊天测试台
   ══════════════════════════════════════════════════════════ */
VIEWS.playground = async (host) => {
  const models = await api("/api/models", { silent: true }).catch(() => ({ models: [] }));
  const list = models.models || [];

  host.innerHTML = `
    <div class="card">
      <div class="card-head"><h2>聊天测试台</h2>
        <span class="muted tiny">走面板内部密钥直连上游，不计入用量统计</span></div>
      <div class="grid g2">
        <label>模型
          <select id="pg-model">
            ${list.length ? list.map((m) => `<option value="${esc(m.id)}">${esc(m.id)}${m.credits ? " · " + esc(m.credits) : ""}</option>`).join("")
              : '<option value="">（上游暂无可用模型）</option>'}
          </select></label>
        <label>系统提示词（可选）<input id="pg-system" placeholder="留空则用上游配置的 prompt 策略"></label>
      </div>
      <label style="margin-top:12px">用户消息
        <textarea id="pg-prompt">你好，请用一句话介绍你自己。</textarea></label>
      <div class="row" style="margin-top:12px">
        <button class="btn primary" id="pg-send">发送</button>
        <label class="inline tiny"><input type="checkbox" id="pg-stream" style="width:auto"> 流式输出</label>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><h3>结果</h3><span id="pg-meta" class="muted tiny"></span></div>
      <div id="pg-out"><div class="empty">还没有发过请求</div></div>
    </div>
  `;

  $("#pg-send", host).addEventListener("click", async (e) => {
    const model = $("#pg-model", host).value;
    const prompt = $("#pg-prompt", host).value.trim();
    if (!model) return toast("上游没有可用模型", "err");
    if (!prompt) return toast("请输入消息", "err");
    const stream = $("#pg-stream", host).checked;
    const out = $("#pg-out", host);
    const meta = $("#pg-meta", host);
    e.target.disabled = true;
    meta.textContent = "请求中…";

    if (!stream) {
      try {
        const r = await api("/api/playground", {
          method: "POST",
          body: { model, prompt, system: $("#pg-system", host).value.trim() },
        });
        if (!r.ok) {
          out.innerHTML = `<pre class="pre error">${esc(JSON.stringify(r.error, null, 2))}</pre>`;
          meta.textContent = `HTTP ${r.status} · ${r.latency_ms} ms`;
        } else {
          out.innerHTML = `
            ${r.reasoning ? `<p class="muted tiny">推理内容</p><pre class="pre">${esc(r.reasoning)}</pre>` : ""}
            <p class="muted tiny">回复</p><pre class="pre">${esc(r.content)}</pre>`;
          meta.textContent = `${r.latency_ms} ms · ${esc(r.model)} · ${JSON.stringify(r.usage)}`;
        }
      } catch (err) { out.innerHTML = `<p class="error">${esc(err.message)}</p>`; }
      finally { e.target.disabled = false; }
      return;
    }

    // 流式：直接用 EventSource 不方便（要 POST），用 fetch + ReadableStream
    out.innerHTML = '<pre class="pre" id="pg-stream-out"></pre>';
    const box = $("#pg-stream-out", host);
    let acc = "", t0 = performance.now();
    try {
      const res = await fetch("/api/playground/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model, prompt, system: $("#pg-system", host).value.trim() }),
        credentials: "same-origin",
      });
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const parts = buf.split("\n\n");
        buf = parts.pop();
        for (const part of parts) {
          const line = part.split("\n").find((l) => l.startsWith("data:"));
          if (!line) continue;
          const payload = line.slice(5).trim();
          if (payload === "[DONE]") continue;
          try {
            const obj = JSON.parse(payload);
            const delta = obj.choices?.[0]?.delta?.content;
            if (delta) { acc += delta; box.textContent = acc; }
          } catch { /* 忽略半截分片 */ }
        }
      }
      meta.textContent = `${Math.round(performance.now() - t0)} ms · 流式`;
    } catch (err) {
      out.innerHTML += `<p class="error">${esc(err.message)}</p>`;
    } finally { e.target.disabled = false; }
  });
};

/* ══════════════════════════════════════════════════════════
   系统设置
   ══════════════════════════════════════════════════════════ */
VIEWS.settings = async (host) => {
  const [s, hint, about, logs, audit] = await Promise.all([
    api("/api/settings"),
    api("/api/settings/schedule-hint"),
    api("/api/system/about"),
    api("/api/system/upstream/logs?limit=200"),
    api("/api/system/audit?limit=60"),
  ]);
  state.settings = s;

  const c = s.config || {};
  const pool = c.pool || {};
  const sticky = c.session_sticky || {};
  const admin = c.admin || {};
  const up = c.upstream || {};

  host.innerHTML = `
    <div class="card">
      <div class="card-head"><h2>常用设置</h2>
        <span class="muted tiny">只提交改动的项；保存后自动重启上游</span></div>
      <div class="grid g3">
        <label>监听地址（上游）
          <input id="s-listen" value="${esc(c.listen || ":7863")}"></label>
        <label class="inline" style="align-self:end">
          <input type="checkbox" id="s-admin" style="width:auto" ${admin.enabled ? "checked" : ""}>
          开启 admin 接口（账号停用/启用/复活需要）
        </label>
        <label>上游 API 密钥（留空=不修改）
          <input id="s-apikey" placeholder="${c.api_key ? "已设置，留空保持不变" : "未设置"}"></label>
        <label>软限流冷却
          <input id="s-cooldown" value="${esc((c.cooldown || {}).soft_rate || "")}" placeholder="如 600s"></label>
        <label>单账号最大在途
          <input id="s-inflight" type="number" min="1" value="${pool.max_in_flight ?? 3}"></label>
        <label>熔断阈值（连续失败）
          <input id="s-breaker" type="number" min="1" value="${pool.breaker_threshold ?? 3}"></label>
        <label class="inline" style="align-self:end">
          <input type="checkbox" id="s-sticky" style="width:auto" ${sticky.enabled ? "checked" : ""}>
          会话粘性（多轮不跳号）
        </label>
        <label>系统提示词模式
          <select id="s-prompt">
            <option value="passthrough" ${(c.prompt || {}).mode === "passthrough" ? "selected" : ""}>passthrough（透传客户端）</option>
            <option value="custom" ${(c.prompt || {}).mode === "custom" ? "selected" : ""}>custom（用文件覆盖）</option>
          </select></label>
        <label>国际版（global）
          <select id="s-global">
            <option value="true" ${(c.global || {}).enabled ? "selected" : ""}>启用</option>
            <option value="false" ${!(c.global || {}).enabled ? "selected" : ""}>关闭</option>
          </select></label>
        <label>HTTP 超时（秒）
          <input id="s-timeout" type="number" min="10" value="${up.timeout_seconds ?? 120}"></label>
        <label>客户端名称
          <input id="s-client" value="${esc(up.client_name || "WorkBuddy")}"></label>
      </div>
      <div class="row" style="margin-top:14px">
        <button class="btn primary" id="s-save">保存并重启上游</button>
        <label class="inline tiny"><input type="checkbox" id="s-norestart" style="width:auto"> 只写配置不重启</label>
      </div>
      <p class="muted tiny" style="margin:10px 0 0">
        配置文件：<code>${esc(s.path)}</code>。修改 <code>api_key</code> 后上游与面板会同时用新值（面板每次调用都重新读取）。
      </p>
    </div>

    <div class="card">
      <div class="card-head"><h2>定时任务</h2><span class="muted tiny">上游 schedule 配置</span></div>
      <div class="table-wrap"><table><thead><tr>
        <th>任务</th><th>状态</th><th>执行时刻</th></tr></thead><tbody>
        ${hint.tasks.map((t) => `<tr>
          <td>${esc(t.label)} <span class="muted tiny mono">${esc(t.key)}_hours</span></td>
          <td><span class="tag ${t.enabled ? "ok" : ""}">${t.enabled ? "开启" : "关闭"}</span></td>
          <td class="tiny">${esc(t.text)}</td>
        </tr>`).join("")}
      </tbody></table></div>
      <p class="muted tiny" style="margin:12px 0 0">
        时刻值在下方「高级」里直接编辑 <code>schedule</code> 数组即可（24 小时制）。
      </p>
    </div>

    <div class="card">
      <div class="card-head"><h2>高级 · 完整配置</h2>
        <span class="muted tiny">慎改；保存会与上面表单一起提交</span></div>
      <p class="muted tiny" style="margin:0 0 8px">
        敏感项（<code>api_key</code> 等）在这里显示为空/占位，保持不动即可；<b>删掉某个键不会生效</b>（合并语义只覆盖不删除）。
      </p>
      <textarea id="s-raw" style="min-height:260px">${esc(JSON.stringify(c, null, 2))}</textarea>
      <div class="row" style="margin-top:10px">
        <button class="btn primary" id="s-save-raw">保存完整配置</button>
        <button class="btn" id="s-reload">重新载入</button>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h2>上游进程日志</h2>
        <span class="muted tiny">${esc(logs.source)} · ${logs.lines.length} 行</span></div>
      <div class="acc-acts" style="margin-bottom:10px">
        <button class="btn tiny" data-log-src="memory">内存缓冲</button>
        <button class="btn tiny" data-log-src="file">日志文件</button>
        <button class="btn tiny" id="log-reload">刷新</button>
      </div>
      ${logs.lines.length === 0 ? '<div class="empty">暂无日志</div>' : `
        <pre class="pre">${logs.lines.map((l) => {
          const cls = /error|失败|panic|fatal/i.test(l) ? "err"
            : /warn|警告|冷却|429/i.test(l) ? "warn" : "";
          return `<span class="log-line ${cls}">${esc(l)}</span>`;
        }).join("\n")}</pre>`}
    </div>

    <div class="grid g2">
      <div class="card">
        <h3>修改面板密码</h3>
        <label>原密码<input id="p-old" type="password" autocomplete="current-password"></label>
        <label style="margin-top:8px">新密码（≥8 位）<input id="p-new" type="password" autocomplete="new-password"></label>
        <button class="btn primary" id="p-save" style="margin-top:12px">修改密码</button>
      </div>
      <div class="card">
        <h3>运行环境</h3>
        <div class="acc-meta">
          <span>面板 <b>v${esc(about.manager_version)}</b></span>
          <span>Python <b>${esc(about.python)}</b></span>
          <span>架构 <b>${esc(about.machine)}</b></span>
          <span>CPU <b>${about.cpu_count} 核</b></span>
        </div>
        <div class="acc-meta" style="margin-top:8px">
          <span>内存可用 <b>${about.memory_mb?.memavailable ?? "—"} MB</b></span>
          <span>磁盘可用 <b>${about.disk?.free_mb ?? "—"} MB</b></span>
        </div>
        <p class="muted tiny" style="margin:12px 0 0">
          上游仓库 <code>${esc(about.upstream_repo)}</code>@<code>${esc((about.upstream_commit || "").slice(0, 10) || "unknown")}</code>。
          升级 = 重新构建镜像并拉取（见 README 的更新流程），面板不会去改上游源码，所以你自己的定制不会被覆盖。
        </p>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h2>审计日志</h2><span class="muted tiny">最近 ${audit.audit.length} 条</span></div>
      <div class="table-wrap"><table><thead><tr>
        <th>时间</th><th>操作者</th><th>动作</th><th>详情</th></tr></thead><tbody>
        ${audit.audit.map((a) => `<tr>
          <td class="tiny">${fmtTime(a.ts)}</td><td>${esc(a.actor)}</td>
          <td class="mono tiny">${esc(a.action)}</td>
          <td class="tiny">${esc(a.detail)}</td>
        </tr>`).join("")}
      </tbody></table></div>
    </div>
  `;

  $("#s-save", host).addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      const r = await api("/api/settings", { method: "PUT", body: { patch: collectForm(host, c), restart: !$("#s-norestart", host).checked } });
      toast(`已保存：${r.changed.join("、") || "无改动"}`, "ok");
      if (r.hint) toast(r.hint, "");
      route();
    } catch (err) { toast(err.message, "err"); }
    finally { e.target.disabled = false; }
  });

  $("#s-save-raw", host).addEventListener("click", async (e) => {
    let parsed;
    try { parsed = JSON.parse($("#s-raw", host).value); }
    catch (err) { return toast("JSON 解析失败：" + err.message, "err"); }
    e.target.disabled = true;
    try {
      await api("/api/settings", { method: "PUT", body: { patch: parsed, restart: true } });
      toast("完整配置已保存，上游重启中", "ok");
      route();
    } catch (err) { toast(err.message, "err"); }
    finally { e.target.disabled = false; }
  });

  $("#s-reload", host).addEventListener("click", route);

  $$("[data-log-src]", host).forEach((b) =>
    b.addEventListener("click", async () => {
      const src = b.dataset.logSrc;
      const r = await api(`/api/system/upstream/logs?limit=200&source=${src}`);
      toast(`已切换到${src === "memory" ? "内存缓冲" : "日志文件"}（${r.lines.length} 行）`, "ok");
      route();
    })
  );
  $("#log-reload", host).addEventListener("click", route);

  $("#p-save", host).addEventListener("click", async (e) => {
    try {
      await api("/api/password", {
        method: "POST",
        body: { old_password: $("#p-old", host).value, new_password: $("#p-new", host).value },
      });
      toast("密码已修改，下次登录用新密码", "ok");
      $("#p-old", host).value = $("#p-new", host).value = "";
    } catch (err) { toast(err.message, "err"); }
  });
};

function collectForm(host, cfg) {
  const patch = {};
  const listen = $("#s-listen", host).value.trim();
  if (listen && listen !== cfg.listen) patch.listen = listen;

  const adminOn = $("#s-admin", host).checked;
  if (adminOn !== !!((cfg.admin || {}).enabled)) patch.admin = { enabled: adminOn };

  const key = $("#s-apikey", host).value.trim();
  if (key) patch.api_key = key;

  const soft = $("#s-cooldown", host).value.trim();
  if (soft && soft !== ((cfg.cooldown || {}).soft_rate || "")) patch.cooldown = { soft_rate: soft };

  const inflight = Number($("#s-inflight", host).value);
  const breaker = Number($("#s-breaker", host).value);
  if (inflight !== (cfg.pool || {}).max_in_flight || breaker !== (cfg.pool || {}).breaker_threshold) {
    patch.pool = { max_in_flight: inflight, breaker_threshold: breaker };
  }

  const stickyOn = $("#s-sticky", host).checked;
  if (stickyOn !== !!(cfg.session_sticky || {}).enabled) patch.session_sticky = { enabled: stickyOn };

  const promptMode = $("#s-prompt", host).value;
  if (promptMode !== ((cfg.prompt || {}).mode || "")) patch.prompt = { mode: promptMode };

  const globalOn = $("#s-global", host).value === "true";
  if (globalOn !== !!(cfg.global || {}).enabled) patch.global = { enabled: globalOn };

  const timeout = Number($("#s-timeout", host).value);
  const client = $("#s-client", host).value.trim();
  if (timeout !== (cfg.upstream || {}).timeout_seconds || client !== ((cfg.upstream || {}).client_name || "")) {
    patch.upstream = { timeout_seconds: timeout, client_name: client };
  }
  return patch;
}

/* ── 通用：卡片里的跳转按钮 ─────────────────────────────── */
function bindNav(root) {
  $$("[data-nav]", root).forEach((b) =>
    b.addEventListener("click", () => { location.hash = "#/" + b.dataset.nav; })
  );
}

/* ── 启动 ──────────────────────────────────────────────── */
async function boot() {
  try {
    state.me = await api("/api/me");
  } catch {
    state.me = null;
  }
  if (!state.me) { showLogin(); return; }
  showApp();
  $("#side-version").textContent = `v${state.me.version} · ${state.me.upstream_repo}`;
  await route();

  // 活跃时静默续签登录态（后端按空闲窗口判失效）
  if (state.refreshTimer) clearInterval(state.refreshTimer);
  state.refreshTimer = setInterval(() => api("/api/session/renew", { method: "POST", silent: true }), 20 * 60 * 1000);
  // 状态药丸定期刷新
  setInterval(renderUpstreamPill, 30 * 1000);
}

boot();
