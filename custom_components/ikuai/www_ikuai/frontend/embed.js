/*!
 * iKuai —— Home Assistant 前端「iKuai 控制台」悬浮窗模块
 *
 * 通过 homeassistant.components.frontend.add_extra_js_url 注入 HA 前端。
 * 设备页「访问」链接指向 /ikuai/web/?entry=…&authSig=…（HA 同源反向代理，
 * 里面是登录后的完整原版 iKuai 界面）。默认点击会整页跳转，这里在捕获阶段
 * 拦截，改为在 HA 页面内弹出可拖动 / 可缩放的选项卡式悬浮窗：
 *
 *   └ 完整后台  ：原版 iKuai Web 100% 功能（拨号/分流/AC/虚拟机/文件…，固件跟随）
 *
 * 悬浮窗只保留「完整后台」一个选项卡（虚拟机控制台走每台虚拟机设备页的独立窗）。
 *
 * 同一套悬浮窗也承载**虚拟机远程控制台**：虚拟机设备页的「访问」链接指向
 * /ikuai/console/<页面令牌>?authSig=…（校验后自动跳到爱快固件原生的 noVNC
 * 控制台页，经同源代理访问），同样在捕获阶段拦截，改为在 HA 页面内开出可拖动 /
 * 可缩放的独立悬浮窗（一台虚拟机一个窗口）。
 * 因为悬浮窗里没有地址栏，工具栏额外提供「复制地址」按钮（http 非安全上下文下
 * 自动退化为 execCommand，保证内网 http 访问也能复制）；关窗时把 iframe 置成
 * about:blank，立刻断开该控制台的 WebSocket / VNC 会话。
 *
 * 另一个关键职责：设备页配置链接里的 host 是 HA 自识别的内网地址，端口转发
 * 访问时不可达。authSig 只绑定 path+query，因此本模块把所有入口链接的 origin
 * 持续改写为浏览器当前地址栏 origin（悬停 / 左键 / 中键 / Ctrl 新标签都正确）。
 *
 * 纯原生 JS + Shadow DOM，不依赖 HA 前端内部实现。
 */
(() => {
  "use strict";

  const PAGE_PATH = "/ikuai/web";
  // 虚拟机控制台页：/ikuai/console/<页面令牌>
  const CONSOLE_PATH = "/ikuai/console";
  // 前缀下这些子路径不是"某台虚拟机的控制台页"，不拦截（面板页/接口/静态资源）
  const CONSOLE_RESERVED = new Set(["assets", "panel", "panel-data"]);
  const HOST_ID = "ikuai-web-float-host";

  const TABS = [
    { id: "full", label: "完整后台", icon: "▦", title: "iKuai 完整管理后台（原版界面）" },
  ];

  const CSS = `
    .ik-window {
      position: fixed;
      z-index: 1;
      display: flex;
      flex-direction: column;
      min-width: 420px;
      min-height: 300px;
      width: min(1320px, 96vw);
      height: min(900px, 92vh);
      background: #f5f7fa;
      border: 1px solid #2f3c4e;
      border-radius: 12px;
      box-shadow: 0 14px 48px rgba(0, 0, 0, .55);
      overflow: hidden;
      resize: both;
      pointer-events: auto;
    }
    /* 虚拟机控制台悬浮窗：深色底、只有一块 iframe，没有选项卡 */
    .ik-window.ik-console {
      width: min(1180px, 96vw);
      height: min(860px, 92vh);
      background: #14161a;
      border-color: #3b4453;
    }
    .ik-console .ik-body { background: #000; }
    .ik-window.ik-flash { animation: ik-pop .3s ease; }
    @keyframes ik-pop {
      0% { transform: scale(.97); }
      60% { transform: scale(1.01); }
      100% { transform: scale(1); }
    }
    .ik-head {
      display: flex;
      align-items: center;
      gap: 8px;
      height: 40px;
      padding: 0 10px;
      background: #1b222d;
      color: #e7edf5;
      flex: 0 0 auto;
    }
    .ik-drag { cursor: move; touch-action: none; flex: 1 1 auto;
      display: flex; align-items: center; gap: 8px; min-width: 0;
      height: 100%; }
    .ik-grip { color: #6b7787; font-size: 14px; letter-spacing: -2px; }
    .ik-title { font-size: 13.5px; font-weight: 600; white-space: nowrap;
      overflow: hidden; text-overflow: ellipsis; }
    .ik-btn {
      appearance: none;
      border: 1px solid #2f3c4e;
      background: #202a38;
      color: #e7edf5;
      border-radius: 7px;
      width: 30px;
      height: 28px;
      font-size: 15px;
      line-height: 1;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      text-decoration: none;
      padding: 0;
      flex: 0 0 auto;
    }
    .ik-btn:hover { background: #2a3646; border-color: #2f9bff; }
    .ik-x:hover { background: #5a2327; border-color: #f0564a; }
    .ik-tabs {
      /* 悬浮窗只剩「完整后台」一个选项卡，标签栏整体隐藏 */
      display: none;
      align-items: stretch;
      gap: 2px;
      height: 38px;
      padding: 0 8px;
      background: #141a22;
      border-bottom: 1px solid #2f3c4e;
      flex: 0 0 auto;
    }
    .ik-tab {
      appearance: none;
      background: transparent;
      border: 0;
      border-bottom: 2px solid transparent;
      color: #9aa7b6;
      font-size: 12.5px;
      padding: 0 14px;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      white-space: nowrap;
    }
    .ik-tab .ik-tab-icon { font-size: 14px; }
    .ik-tab:hover { color: #e7edf5; }
    .ik-tab.ik-active {
      color: #fff;
      border-bottom-color: #2f9bff;
      background: linear-gradient(180deg, transparent 70%, rgba(47,155,255,.18));
    }
    .ik-tab[hidden] { display: none; }
    .ik-body { flex: 1 1 auto; position: relative; background: #fff; }
    .ik-frame { position: absolute; inset: 0; width: 100%; height: 100%;
      border: 0; background: #fff; }
    .ik-frame[hidden] { display: none; }
    .ik-mask {
      position: absolute; inset: 0; z-index: 5;
      display: none; flex-direction: column; align-items: center; justify-content: center;
      gap: 14px; padding: 28px; text-align: center;
      background: #f5f7fa; color: #2f3c4e;
    }
    .ik-mask.ik-show { display: flex; }
    .ik-mask-icon { font-size: 40px; color: #c0ccda; }
    .ik-mask-msg { font-size: 14px; line-height: 1.7; max-width: 440px;
      white-space: pre-line; }
    .ik-mask-code { font-size: 12px; color: #8492a6; }
    .ik-mask-btn {
      appearance: none; border: 0; border-radius: 8px; cursor: pointer;
      background: #2f9bff; color: #fff; font-size: 13.5px; font-weight: 600;
      padding: 9px 22px;
    }
    .ik-mask-btn:hover { background: #1a85ea; }
    .ik-mask-btn.ik-sec { background: #e7edf5; color: #2f3c4e; }
    .ik-mask-btn.ik-sec:hover { background: #d8e0ea; }

    /* ---------- 窄屏：手机 / 平板竖屏 / HA App 手机版 ---------- */
    /* 手机上悬浮窗接近满屏；min-width 必须放开，否则桌面用的 420px 会把
       360~430px 宽的屏幕撑出横向滚动（控制台画面直接被推到屏幕外） */
    @media (max-width: 720px) {
      .ik-window {
        min-width: 0;
        min-height: 0;
        width: 98vw;
        height: 86vh;
        border-radius: 10px;
        resize: none;                 /* 触屏上拖右下角改大小很别扭 */
      }
      .ik-window.ik-console { width: 98vw; height: 92vh; }
      .ik-head { height: 44px; padding: 0 8px; gap: 6px; }
      .ik-drag { gap: 6px; }
      .ik-grip { display: none; }     /* 手机上不需要"抓手"提示，省位置 */
      .ik-title { font-size: 12.5px; }
      .ik-btn { width: 38px; height: 34px; font-size: 16px; }   /* 手指按得住 */
      /* 选项卡横滑，不换行、不挤标题 */
      .ik-tabs {
        height: 44px;
        padding: 0 6px;
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
      }
      .ik-tab { flex: 0 0 auto; padding: 0 10px; font-size: 12.5px; }
      .ik-mask { padding: 18px; gap: 10px; }
      .ik-mask-icon { font-size: 32px; }
      .ik-mask-msg { font-size: 13px; }
      .ik-mask-btn { padding: 11px 20px; font-size: 14px; }
    }
    /* 手机横屏：高度太紧时窗口再矮一点，别把 HA 自己的顶栏顶掉 */
    @media (max-height: 460px) {
      .ik-window { height: 94vh; }
    }
  `;

  // ---- 链接识别：穿透设备页 Shadow DOM 找到「访问」入口 --------------------
  // 返回 { url, node, kind }：kind = "web"（路由器后台）/ "console"（虚拟机控制台）
  function toBrowserOrigin(u) {
    // 以浏览器当前地址栏 origin 为准（端口映射场景链接里的 host 不可达；
    // authSig 只签 path+query，换 host 仍有效）
    try { return new URL(u.pathname + u.search, window.location.origin); }
    catch (_) { return u; }   // 解析失败用原链接
  }

  function matchConsoleUrl(u) {
    if (!u.pathname.startsWith(CONSOLE_PATH + "/")) { return null; }
    const tail = u.pathname.slice(CONSOLE_PATH.length + 1).replace(/\/+$/, "");
    // 只认"某台虚拟机一页"：单个路径段，且不是已知的子资源
    if (!tail || tail.includes("/") || CONSOLE_RESERVED.has(tail)) { return null; }
    if (!u.searchParams.has("authSig")) { return null; }
    return toBrowserOrigin(u);
  }

  function matchFromPath(path) {
    for (const node of path) {
      if (!node || node.nodeType !== 1) { continue; }
      let href = null;
      if (node.tagName === "A") { href = node.href; }
      if (!href && typeof node.getAttribute === "function") {
        href = node.getAttribute("href");
      }
      if (!href) { continue; }
      let u;
      try { u = new URL(href, location.href); } catch (_) { continue; }
      if (u.pathname === PAGE_PATH || u.pathname === PAGE_PATH + "/") {
        if (!u.searchParams.has("authSig")) { continue; }
        return { url: toBrowserOrigin(u), node, kind: "web" };
      }
      const consoleUrl = matchConsoleUrl(u);
      if (consoleUrl) {
        return { url: consoleUrl, node, kind: "console" };
      }
    }
    return null;
  }

  function proxyUrlFromEvent(e) {
    let path = null;
    try { path = typeof e.composedPath === "function" ? e.composedPath() : null; }
    catch (_) { path = null; }
    if (!path || !path.length) {
      path = [];
      let n = e.target;
      while (n) {
        path.push(n);
        const root = n.getRootNode ? n.getRootNode() : null;
        n = n.parentNode || (root && root.host) || null;
      }
    }
    return matchFromPath(path);
  }

  // ---- 持续改写设备页链接 origin（悬停/中键/Ctrl 新标签也用当前地址） ------
  function* deepAnchors(root, out = []) {
    for (const el of root.querySelectorAll("a[href]")) { yield el; }
    const all = root.querySelectorAll("*");
    for (const el of all) {
      if (el.shadowRoot) { yield* deepAnchors(el.shadowRoot, out); }
    }
  }

  function anchorToBrowserOrigin(a) {
    let u;
    try { u = new URL(a.getAttribute("href"), location.href); }
    catch (_) { return false; }
    const isWeb = u.pathname === PAGE_PATH || u.pathname === PAGE_PATH + "/";
    const isConsole = !!matchConsoleUrl(u);
    if (!isWeb && !isConsole) { return false; }
    if (!u.searchParams.has("authSig")) { return false; }
    if (u.origin === window.location.origin) { return false; }
    try {
      const fixed = new URL(u.pathname + u.search, window.location.origin);
      a.setAttribute("href", fixed.href);
      return true;
    } catch (_) { return false; }
  }

  function sweepAnchors() {
    try {
      for (const a of deepAnchors(document)) {
        try { anchorToBrowserOrigin(a); } catch (_) { /* 忽略单个节点 */ }
      }
    } catch (_) { /* 文档未就绪等 */ }
  }
  setInterval(sweepAnchors, 2000);
  document.addEventListener("DOMContentLoaded", sweepAnchors);

  // ---- 地址栏来源心跳（让设备链接/远程地址传感器跟随外部访问地址） ----
  // 服务端只知道 HA 内网地址，端口转发/域名下的地址栏 host:port 只有浏览器
  // 知道。这里用 HA 登录态 GET 一次心跳端点，后端直接读请求 Host 头学习，
  // 无需 body。必须带鉴权：裸 fetch 会被 HA 记为失败登录，攒够次数封 IP。
  const ORIGIN_PATH = "/ikuai/api/origin";
  let lastOriginReport = 0;
  let originRetries = 0;

  function authFetcher() {
    try {
      const root = document.querySelector("home-assistant");
      const hass = root && root.hass;
      if (!hass) { return null; }
      if (typeof hass.fetchWithAuth === "function") {
        return (url, init) => hass.fetchWithAuth(url, init);
      }
      const token = hass.auth && hass.auth.data && hass.auth.data.access_token;
      if (token) {
        return (url, init) => fetch(url, {
          ...init,
          headers: { ...(init && init.headers), Authorization: "Bearer " + token }
        });
      }
    } catch (_) { /* 取不到就当作未登录 */ }
    return null;
  }

  function reportOrigin() {
    const now = Date.now();
    if (now - lastOriginReport < 30000) { return; }
    const fetcher = authFetcher();
    if (!fetcher) {
      // 本模块常先于 HA 前端就绪：有限次重试，不无限轮询
      if (originRetries < 10) {
        originRetries += 1;
        setTimeout(reportOrigin, 1500);
      }
      return;
    }
    originRetries = 0;
    lastOriginReport = now;
    try {
      fetcher(ORIGIN_PATH, {
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        headers: { "Cache-Control": "no-store" }
      }).catch(() => {});
    } catch (_) { /* 页面卸载等场景，静默 */ }
  }
  reportOrigin();
  window.addEventListener("focus", reportOrigin);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") { reportOrigin(); }
  });
  window.addEventListener("pageshow", reportOrigin);

  // ---- 悬浮窗 -------------------------------------------------------------
  function ensureHost() {
    let host = document.getElementById(HOST_ID);
    if (host) { return host; }
    host = document.createElement("div");
    host.id = HOST_ID;
    // 容器不挡鼠标，只有窗体接收事件：非模态，窗外照常操作 HA
    host.style.cssText = "position:fixed;inset:0;z-index:999999;pointer-events:none";
    const shadow = host.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = CSS;
    shadow.appendChild(style);
    document.body.appendChild(host);
    return host;
  }

  // 窄屏（手机）：窗口 CSS 本身已是 98vw / 86~92vh，这里只需贴边放。
  // 桌面那套"垂直居中 - 10px"在手机上会把标题栏挤到屏幕外（手机高度很紧）。
  function isNarrowScreen() {
    return window.innerWidth <= 720;
  }

  function centerWindow(win) {
    if (isNarrowScreen()) {
      win.style.left = Math.max(2, Math.round(window.innerWidth * 0.01)) + "px";
      win.style.top = Math.max(2, Math.round(window.innerHeight * 0.04)) + "px";
      return;
    }
    const w = win.offsetWidth || Math.min(1320, window.innerWidth * 0.96);
    const h = win.offsetHeight || Math.min(900, window.innerHeight * 0.92);
    win.style.left = Math.max(8, (window.innerWidth - w) / 2) + "px";
    win.style.top = Math.max(8, (window.innerHeight - h) / 2 - 10) + "px";
  }

  function enableDrag(win, handle) {
    handle.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".ik-btn")) { return; }
      e.preventDefault();
      const offX = e.clientX - win.offsetLeft;
      const offY = e.clientY - win.offsetTop;
      const move = (ev) => {
        const x = Math.max(-win.offsetWidth + 120,
          Math.min(window.innerWidth - 60, ev.clientX - offX));
        const y = Math.max(0,
          Math.min(window.innerHeight - 40, ev.clientY - offY));
        win.style.left = x + "px";
        win.style.top = y + "px";
      };
      const up = () => {
        document.removeEventListener("pointermove", move);
        document.removeEventListener("pointerup", up);
      };
      document.addEventListener("pointermove", move);
      document.addEventListener("pointerup", up);
    });
  }

  // ---- 复制到剪贴板（http 内网访问时没有 navigator.clipboard，退化到 execCommand）
  function copyToClipboard(text, btn) {
    const done = () => {
      if (!btn || btn.dataset.copied === "1") { return; }
      btn.dataset.copied = "1";
      const old = btn.textContent;
      const oldTitle = btn.title;
      btn.textContent = "✓";
      btn.title = "已复制";
      setTimeout(() => {
        btn.dataset.copied = "";
        btn.textContent = old;
        btn.title = oldTitle;
      }, 1200);
    };
    const fallback = () => {
      try {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.cssText = "position:fixed;top:-1000px;left:-1000px;opacity:0";
        document.body.appendChild(ta);
        ta.select();
        ta.setSelectionRange(0, ta.value.length);
        const ok = document.execCommand("copy");
        ta.remove();
        if (ok) { done(); }
      } catch (_) { /* 复制不了时不动按钮 */ }
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(fallback);
    } else {
      fallback();
    }
  }

  // ---- 虚拟机控制台悬浮窗（一台虚拟机一个窗口） ----------------------------
  const consoleWindows = new Map();   // 页面令牌 -> 窗口

  function consoleToken(u) {
    return u.pathname.slice(CONSOLE_PATH.length + 1).replace(/\/+$/, "");
  }

  function openConsole(url) {
    sweepAnchors();
    const host = ensureHost();
    const root = host.shadowRoot;
    const token = consoleToken(url);

    let win = consoleWindows.get(token);
    if (win && !win.isConnected) { consoleWindows.delete(token); win = null; }

    if (win) {
      const frame = win.querySelector('[data-frame="console"]');
      if (frame.dataset.url !== url.href) {
        frame.dataset.url = url.href;
        frame.src = url.href;
      }
      win.querySelector(".ik-link").setAttribute("href", url.href);
      win.style.display = "flex";
      win.classList.remove("ik-flash");
      void win.offsetWidth;
      win.classList.add("ik-flash");
      return;
    }

    win = document.createElement("div");
    win.className = "ik-window ik-console";
    win.innerHTML = `
      <div class="ik-head">
        <div class="ik-drag">
          <span class="ik-grip">⠿</span>
          <span class="ik-title">iKuai · 虚拟机控制台</span>
        </div>
        <button type="button" class="ik-btn" data-act="copy" title="复制控制台地址">⧉</button>
        <button type="button" class="ik-btn" data-act="reload" title="重新加载控制台">↻</button>
        <a class="ik-btn ik-link" target="_blank" rel="noopener" title="在新标签页打开">↗</a>
        <button type="button" class="ik-btn ik-x" data-act="close"
          title="关闭（同时断开该控制台会话）">✕</button>
      </div>
      <div class="ik-body">
        <iframe class="ik-frame" data-frame="console"
          allow="fullscreen; clipboard-read; clipboard-write; modals"
          allowfullscreen></iframe>
      </div>`;
    root.appendChild(win);

    const frame = win.querySelector('[data-frame="console"]');
    frame.dataset.url = url.href;
    frame.src = url.href;
    win.querySelector(".ik-link").setAttribute("href", url.href);

    centerWindow(win);
    // 多个悬浮窗（后台 + 若干台虚拟机控制台）同时开着时错开一点，别完全叠在一起；
    // 手机上窗口本来就铺满，错开只会把标题栏推出屏幕
    const opened = root.querySelectorAll(".ik-window").length;
    if (opened > 1 && !isNarrowScreen()) {
      win.style.left = (parseFloat(win.style.left) + 26 * (opened - 1)) + "px";
      win.style.top = (parseFloat(win.style.top) + 24 * (opened - 1)) + "px";
    }
    enableDrag(win, win.querySelector(".ik-drag"));

    win.querySelector('[data-act="copy"]').addEventListener("click", (e) => {
      copyToClipboard(frame.dataset.url, e.currentTarget);
    });
    win.querySelector('[data-act="reload"]').addEventListener("click", () => {
      frame.src = frame.dataset.url;
    });
    win.querySelector('[data-act="close"]').addEventListener("click", () => {
      // 置空再移除：立刻断开控制台的 WebSocket 与 VNC 连接
      frame.src = "about:blank";
      win.remove();
      consoleWindows.delete(token);
    });

    consoleWindows.set(token, win);
    win.classList.add("ik-flash");
  }

  function openProxy(url) {
    sweepAnchors();
    const host = ensureHost();
    const root = host.shadowRoot;
    let win = root.querySelector(".ik-window.ik-web");

    if (win) {
      win._fullUrl = url.href;
      const full = win.querySelector('[data-frame="full"]');
      if (full.dataset.url !== url.href) {
        full.dataset.url = url.href;
        if (full.src) { full.src = url.href; }
      }
      win.style.display = "flex";
      win.classList.remove("ik-flash");
      void win.offsetWidth;
      win.classList.add("ik-flash");
      activateTab(win, win._active || "full");
      return;
    }

    win = document.createElement("div");
    win.className = "ik-window ik-web";
    win._fullUrl = url.href;
    win._active = "full";
    win.innerHTML = `
      <div class="ik-head">
        <div class="ik-drag">
          <span class="ik-grip">⠿</span>
          <span class="ik-title"></span>
        </div>
        <button type="button" class="ik-btn" data-act="copy" title="复制地址">⧉</button>
        <button type="button" class="ik-btn" data-act="reload" title="重新加载当前页">↻</button>
        <a class="ik-btn ik-link" target="_blank" rel="noopener" title="在新标签页打开">↗</a>
        <button type="button" class="ik-btn ik-x" data-act="close" title="关闭">✕</button>
      </div>
      <div class="ik-tabs">
        ${TABS.map((t) => `<button type="button" class="ik-tab${t.id === "full" ? " ik-active" : ""}"
            data-tab="${t.id}" title="${t.title}"${t.hidden ? " hidden" : ""}>
            <span class="ik-tab-icon">${t.icon}</span>${t.label}</button>`).join("")}
      </div>
      <div class="ik-body">
        <iframe class="ik-frame" data-frame="full"
          allow="fullscreen; clipboard-read; clipboard-write; modals"
          allowfullscreen></iframe>
        <div class="ik-mask" data-mask>
          <div class="ik-mask-icon">⚠</div>
          <div class="ik-mask-msg" data-mask-msg></div>
          <div class="ik-mask-code" data-mask-code></div>
          <div style="display:flex;gap:10px">
            <button type="button" class="ik-mask-btn" data-mask-renew>刷新页面续期</button>
            <button type="button" class="ik-mask-btn ik-sec" data-mask-retry>重试</button>
          </div>
        </div>
      </div>`;
    root.appendChild(win);

    const full = win.querySelector('[data-frame="full"]');
    full.dataset.url = url.href;
    full.src = url.href;

    // ---- 入口凭证失效检测（HA 重启后 authSig/cookie 密钥轮换，旧页面里的
    //      签名链接会 401，iframe 表现为空白；探测到后给出续期提示） ----
    const mask = win.querySelector("[data-mask]");
    function showMask(msg, code, canRenew) {
      mask.querySelector("[data-mask-msg]").textContent = msg;
      mask.querySelector("[data-mask-code]").textContent = code || "";
      mask.querySelector("[data-mask-renew]").style.display = canRenew ? "" : "none";
      mask.classList.add("ik-show");
    }
    function hideMask() { mask.classList.remove("ik-show"); }

    async function probeEntry() {
      const probe = full.dataset.url;
      if (!probe || win._active !== "full") { return; }
      try {
        const resp = await fetch(probe, {
          method: "GET", cache: "no-store", credentials: "same-origin",
        });
        if (resp.ok) { hideMask(); return; }
        if (resp.status === 401) {
          showMask("访问凭证已失效（通常因 Home Assistant 重启或登录态过期）。\n"
            + "点击下方按钮刷新 HA 页面，即可自动获取新凭证后重新打开。",
            "HTTP 401（authSig/cookie 失效）", true);
        } else if (resp.status === 502) {
          showMask("代理到路由器的登录失败：无法连上游或账号密码被拒。\n"
            + "请检查集成配置与路由器连通性后重试。", "HTTP 502", false);
        } else {
          showMask("代理返回异常，可点「重试」重新加载。", "HTTP " + resp.status, false);
        }
      } catch (err) {
        showMask("无法连接到代理服务（网络中断或端口转发不可达？）。", String(err), false);
      }
    }
    full.addEventListener("load", () => setTimeout(probeEntry, 400));
    win._probeEntry = probeEntry;
    mask.querySelector("[data-mask-renew]").addEventListener("click", () => {
      location.reload();
    });
    mask.querySelector("[data-mask-retry]").addEventListener("click", () => {
      hideMask();
      full.src = full.dataset.url;
    });

    centerWindow(win);
    enableDrag(win, win.querySelector(".ik-drag"));

    win.querySelector('[data-act="copy"]').addEventListener("click", (e) => {
      copyToClipboard(full.dataset.url || "", e.currentTarget);
    });
    win.querySelector('[data-act="reload"]').addEventListener("click", () => {
      full.src = full.dataset.url;
    });
    win.querySelector('[data-act="close"]').addEventListener("click", () => {
      win.querySelectorAll("iframe").forEach((f) => { f.src = "about:blank"; });
      win.remove();
    });

    win.querySelectorAll(".ik-tab").forEach((btn) => {
      btn.addEventListener("click", () => activateTab(win, btn.dataset.tab));
    });

    activateTab(win, "full");
    win.classList.add("ik-flash");
  }

  function activateTab(win, id) {
    // 悬浮窗现在只有「完整后台」一个选项卡，这里仅维护标题/外链/凭证探测
    win._active = "full";
    win.querySelectorAll(".ik-tab").forEach((b) => {
      b.classList.toggle("ik-active", b.dataset.tab === "full");
    });
    const f = win.querySelector('.ik-frame[data-frame="full"]');
    win.querySelector(".ik-title").textContent = "iKuai · 完整后台";
    const link = win.querySelector(".ik-link");
    if (f && link) { link.href = f.dataset.url || ""; }
    if (typeof win._probeEntry === "function") { win._probeEntry(); }
  }

  // ---- 点击拦截 -----------------------------------------------------------
  function onClick(e) {
    const match = proxyUrlFromEvent(e);
    if (!match) { return; }
    // 顺手把 href 改成当前 origin，修饰键走原生新标签时也可达
    try {
      if (match.node.getAttribute && match.node.getAttribute("href")) {
        match.node.setAttribute("href", match.url.href);
      }
    } catch (_) { /* 改不动属性时静默 */ }
    if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) {
      return;  // 修饰键/中键：浏览器原生新标签
    }
    e.preventDefault();
    e.stopPropagation();
    reportOrigin();
    // 虚拟机控制台 / 路由器后台各走各的悬浮窗
    if (match.kind === "console") { openConsole(match.url); }
    else { openProxy(match.url); }
  }
  document.addEventListener("click", onClick, true);

  window.__ikuaiWebEmbed = "2026.10.12.3";
  // 链接识别的探针：浏览器控制台里可直接查"这个链接为什么没被悬浮窗接管"，
  // 集成自测脚本也用它跑纯函数断言。
  window.__ikuaiWebEmbedProbe = { matchFromPath, matchConsoleUrl, consoleToken };
})();
