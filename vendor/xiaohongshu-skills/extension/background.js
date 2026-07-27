/**
 * XHS Bridge - Background Service Worker
 *
 * 连接 Python bridge server（ws://localhost:9333），接收命令并执行：
 * - navigate / wait_for_load: chrome.tabs.update + onUpdated
 * - evaluate / has_element 等: chrome.scripting.executeScript (MAIN world)
 * - click / input 等 DOM 操作: chrome.tabs.sendMessage → content.js
 * - screenshot: chrome.tabs.captureVisibleTab
 * Tonyredbook fork: no cookie, request-header, NetLog, or fingerprint collection.
 */

importScripts("tab_selection.js");
const { isXhsUrl, isHealthyContext, sortTabsByRecency } = TonyredbookTabSelection;

const BRIDGE_URL = "ws://localhost:9333";
const TONYREDBOOK_EXTENSION_BUILD = "tonyredbook-0.4.0-rc3";
let ws = null;
let forwardNavigationCount = 0;
let historyBackCount = 0;
let boundXhsTabId = null;

// Keep the MV3 worker and its local-only Bridge socket recoverable.  Chrome
// may suspend an idle service worker even when a WebSocket object still looks
// OPEN in memory.  A local ping every 20 seconds keeps the socket active; the
// minimum 30-second alarm wakes a suspended worker and reconnects it.
const KEEP_ALIVE_ALARM = "tonyredbookBridgeKeepAlive";
function keepBridgeAlive() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    try {
      ws.send(JSON.stringify({
        type: "keepalive",
        build_id: TONYREDBOOK_EXTENSION_BUILD,
      }));
      return;
    } catch {
      ws = null;
    }
  }
  connect();
}
setInterval(keepBridgeAlive, 20000);
chrome.alarms.create(KEEP_ALIVE_ALARM, {
  delayInMinutes: 0.5,
  periodInMinutes: 0.5,
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (!alarm || alarm.name === KEEP_ALIVE_ALARM) keepBridgeAlive();
});

// ───────────────────────── WebSocket ─────────────────────────

function setStatus(connected) {
  chrome.storage.session.set({ wsConnected: connected });
}

function broadcastStatus() {
  const status = { wsConnected: ws !== null && ws.readyState === WebSocket.OPEN };
  chrome.runtime.sendMessage({ type: "STATUS_CHANGED", status }).catch(() => {});
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "GET_STATUS") {
    sendResponse({
      success: true,
      status: { wsConnected: ws !== null && ws.readyState === WebSocket.OPEN },
    });
    return true;
  }

  if (msg.type === "ANALYZE_RISK_CONTROL") {
    sendResponse({ error: "disabled_by_tonyredbook_privacy_policy" });
    return true;
  }

});

function connect() {
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;

  ws = new WebSocket(BRIDGE_URL);

  ws.onopen = async () => {
    console.log("[XHS Bridge] 已连接到 bridge server");
    const extensionInstanceId = await getExtensionInstanceId();
    ws.send(JSON.stringify({
      role: "extension",
      build_id: TONYREDBOOK_EXTENSION_BUILD,
      extension_instance_id: extensionInstanceId,
    }));
    setStatus(true);
    broadcastStatus();
  };

  ws.onmessage = async (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }
    try {
      const result = await handleCommand(msg);
      ws.send(JSON.stringify({ id: msg.id, result: result ?? null }));
    } catch (err) {
      ws.send(JSON.stringify({ id: msg.id, error: String(err.message || err) }));
    }
  };

  ws.onclose = () => {
    console.log("[XHS Bridge] 连接断开，3s 后重连...");
    setStatus(false);
    broadcastStatus();
    setTimeout(connect, 3000);
  };

  ws.onerror = (e) => {
    console.error("[XHS Bridge] WS 错误", e);
  };
}

async function getExtensionInstanceId() {
  const key = "tonyredbookExtensionInstanceId";
  const existing = await chrome.storage.local.get(key);
  if (typeof existing?.[key] === "string" && existing[key]) return existing[key];
  const created = crypto.randomUUID();
  await chrome.storage.local.set({ [key]: created });
  return created;
}

// ───────────────────────── 命令路由 ─────────────────────────

async function handleCommand(msg) {
  const { method, params = {} } = msg;

  switch (method) {
    case "bind_active_xhs_tab":
      return await cmdBindActiveXhsTab();

    case "list_xhs_tabs":
      return await cmdListXhsTabs();

    // ── 导航 ──
    case "navigate":
      return await cmdNavigate(params);

    case "wait_for_load":
      return await cmdWaitForLoad(params);

    case "get_page_context":
      return await cmdGetPageContext();

    case "go_back_and_verify":
      return await cmdGoBackAndVerify(params);

    case "return_to_source_note":
      return await cmdReturnToSourceNote(params);

    case "return_to_profile":
      return await cmdReturnToProfile(params);

    case "open_search_result":
      throw new Error("legacy combined open_search_result is disabled; use semantic_scroll then exact click");

    case "get_navigation_count":
      return { forward: forwardNavigationCount, back: historyBackCount };

    // ── 截图 ──
    case "screenshot_element":
      return await cmdScreenshot(params);

    case "set_file_input":
      return await cmdSetFileInputViaDebugger(params);

    case "click_element":
    case "click_nth_element":
    case "click_element_by_text":
      return await cmdClickViaDebugger(method, params);

    case "press_key":
      return await cmdPressKeyViaDebugger(params);

    case "type_text":
      return await cmdTypeTextViaDebugger(params);

    case "input_content_editable_progressive":
      return await cmdInputContentEditableViaDebugger(params);

    case "semantic_scroll":
      return await cmdSemanticScrollViaDebugger(params);

    case "pointer_event":
      return await cmdPointerEventViaDebugger(params);

    case "simulate_reading":
      return await cmdSimulateReadingViaDebugger(params);

    // Fail closed: these legacy MAIN-world actions bypass the audited input,
    // pointer and scroll primitives above.
    case "input_text":
    case "input_content_editable":
    case "scroll_by":
    case "scroll_to":
    case "scroll_to_bottom":
    case "scroll_element_into_view":
    case "scroll_nth_element_into_view":
    case "dispatch_wheel_event":
    case "mouse_move":
    case "mouse_click":
      throw new Error(`legacy direct DOM action is disabled: ${method}`);

    // ── 风控分析 ──
    case "analyze_risk_control":
      return { error: "disabled_by_tonyredbook_privacy_policy" };

    // ── 在页面主 world 执行 JS（可访问 window.__INITIAL_STATE__ 等） ──
    case "evaluate":
    case "wait_dom_stable":
    case "wait_for_selector":
    case "has_element":
    case "get_elements_count":
    case "get_element_text":
    case "get_element_attribute":
    case "get_scroll_top":
    case "get_viewport_height":
    case "get_url":
    case "get_elements_info":
    case "get_iframe_text":
      return await cmdEvaluateInMainWorld(method, params);

    default:
      throw new Error(`unsupported Bridge method: ${method}`);
  }
}

// ───────────────────────── 导航 ─────────────────────────

/**
 * 导航完成后立即在 MAIN world 检测页面是否为 404 / 风控拦截页。
 * 这是捕获导航级 404 的唯一可靠时机（fetch/XHR 拦截器看不到 browser navigation）。
 */

async function cmdNavigate({ url }) {
  const tab = await getOrOpenXhsTab();
  forwardNavigationCount += 1;
  await chrome.tabs.update(tab.id, { url });
  await waitForTabComplete(tab.id, url, 60000);
  const finalTab = await chrome.tabs.get(tab.id).catch(() => null);
  const finalUrl = finalTab?.url || "";
  if (/xiaohongshu\.com\/404(\?|$)/.test(finalUrl)) {
    throw new Error("页面不可访问或已重定向到错误页");
  }
  const risk = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "MAIN",
    func: () => {
      const text = document.body?.innerText || "";
      return [
        "验证码", "异常登录", "操作频繁", "账号存在风险", "功能受限",
        "禁言", "违反社区规范",
      ]
        .filter(term => text.includes(term));
    },
  }).catch(() => []);
  const signals = risk?.[0]?.result || [];
  if (signals.length) throw new Error(`页面风险提示: ${signals.join(",")}`);
  return null;
}

async function cmdGetPageContext() {
  const tab = await getOrOpenXhsTab();
  return { ...(await readPageContext(tab.id)), boundTabId: tab.id };
}

async function cmdBindActiveXhsTab() {
  const activeTabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  const activeTab = activeTabs.find(isXhsTab);
  if (activeTab?.id && await isHealthyXhsTab(activeTab)) {
    boundXhsTabId = activeTab.id;
    return { ...(await readPageContext(activeTab.id)), boundTabId: activeTab.id, bindingSource: "active_tab" };
  }
  const tabs = await queryAllXhsTabs();
  const tab = await findHealthyRecentXhsTab(tabs);
  if (!tab?.id) throw new Error("没有可绑定的健康小红书标签");
  boundXhsTabId = tab.id;
  return { ...(await readPageContext(tab.id)), boundTabId: tab.id, bindingSource: "healthy_recent_tab" };
}

async function cmdListXhsTabs() {
  const tabs = await queryAllXhsTabs();
  const result = [];
  for (const tab of tabs) {
    try {
      const context = await readPageContext(tab.id);
      result.push({
        tabId: tab.id,
        windowId: tab.windowId,
        active: tab.active === true,
        lastAccessed: Number(tab.lastAccessed || 0),
        origin: context.origin || "",
        pathname: context.pathname || "",
        pageType: context.pageType || "other",
        noteId: context.noteId || "",
        query: context.query || "",
        riskSignals: context.riskSignals || [],
      });
    } catch {
      result.push({
        tabId: tab.id,
        windowId: tab.windowId,
        active: tab.active === true,
        lastAccessed: Number(tab.lastAccessed || 0),
        origin: "",
        pathname: "",
        pageType: "unreadable",
        noteId: "",
        query: "",
        riskSignals: ["page_context_unreadable"],
      });
    }
  }
  return { tabs: result, count: result.length };
}

async function readPageContext(tabId) {
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    func: () => {
      const url = new URL(location.href);
      const path = url.pathname;
      const pathMatch = path.match(/\/(?:explore|search_result)\/([a-zA-Z0-9_-]+)/);
      const noteId = pathMatch?.[1] || "";
      let pageType = "other";
      const profileMatch = path.match(/^\/user\/profile\/([a-zA-Z0-9_-]+)(?:\/|$)/);
      const profileId = profileMatch?.[1] || "";
      const messageEditors = Array.from(document.querySelectorAll([
        'textarea[placeholder*="发送消息"]',
        'textarea[placeholder*="发消息"]',
        '[contenteditable="true"][data-placeholder*="发送消息"]',
        '[contenteditable="true"][data-placeholder*="发消息"]',
        '[contenteditable="true"][placeholder*="发送消息"]',
        '[contenteditable="true"][placeholder*="发消息"]',
      ].join(',')));
      const messageEditorVisible = messageEditors.some((node) => {
        const rect = node.getBoundingClientRect();
        const style = getComputedStyle(node);
        return rect.width > 0 && rect.height > 0
          && rect.bottom > 0 && rect.right > 0
          && rect.top < innerHeight && rect.left < innerWidth
          && style.visibility !== 'hidden' && style.display !== 'none';
      });
      if (noteId) pageType = "note_detail";
      else if (path === "/search_result" || path.startsWith("/search_result/")) pageType = "search_results";
      else if (messageEditorVisible) pageType = "dm_conversation";
      else if (path === "/notification" || path.startsWith("/notification/")) pageType = "message_inbox";
      else if (path.startsWith("/messages") || path.startsWith("/im") || path.startsWith("/chat")) pageType = "message_surface";
      else if (profileId) pageType = "profile";
      else if (path === "/explore" || path === "/") pageType = "home";
      const searchInput = document.querySelector('input[type="search"], input[placeholder*="搜索"], textarea.textarea, textarea[placeholder*="搜索"]');
      const query = String(searchInput?.value || url.searchParams.get("keyword") || "").trim();
      const text = document.body?.innerText || "";
      const riskSignals = [
        "验证码", "异常登录", "操作频繁", "账号存在风险", "功能受限",
        "禁言", "违反社区规范",
      ].filter((term) => text.includes(term));
      const unavailableNote = text.includes("当前笔记暂时无法浏览");
      if (unavailableNote) {
        riskSignals.push("当前笔记暂时无法浏览");
        if (text.includes("扫码查看")) riskSignals.push("扫码查看");
        if (text.includes("打开小红书App扫码")) riskSignals.push("打开小红书App扫码");
      }
      return {
        origin: url.origin,
        pathname: path,
        pageType,
        noteId,
        profileId,
        messageEditorVisible,
        query,
        riskSignals,
      };
    },
  });
  const context = results?.[0]?.result || {};
  return {
    ...context,
    navigationCount: { forward: forwardNavigationCount, back: historyBackCount },
  };
}

async function cmdGoBackAndVerify({ expectedQuery = "" } = {}) {
  const tab = await getOrOpenXhsTab();
  historyBackCount += 1;
  await chrome.tabs.goBack(tab.id);
  await waitForTabComplete(tab.id, null, 60000);
  const context = await readPageContext(tab.id);
  if (context.pageType !== "search_results") {
    throw new Error("返回后不是搜索结果页");
  }
  if (expectedQuery && context.query !== expectedQuery) {
    throw new Error("返回后的搜索词与会话不匹配");
  }
  if (context.riskSignals?.length) {
    throw new Error(`页面风险提示: ${context.riskSignals.join(",")}`);
  }
  return { ...context, boundTabId: tab.id };
}

async function cmdReturnToSourceNote({ expectedNoteId = "" } = {}) {
  if (!/^[A-Za-z0-9_-]+$/.test(expectedNoteId)) {
    throw new Error("返回原帖需要合法的 expectedNoteId");
  }
  const tab = await getOrOpenXhsTab();
  historyBackCount += 1;
  await chrome.tabs.goBack(tab.id);
  await waitForTabComplete(tab.id, null, 60000);
  const context = await readPageContext(tab.id);
  if (context.pageType !== "note_detail" || context.noteId !== expectedNoteId) {
    throw new Error("返回后不是预期原帖；拒绝直链补偿");
  }
  if (context.riskSignals?.length) {
    throw new Error(`页面风险提示: ${context.riskSignals.join(",")}`);
  }
  return { ...context, boundTabId: tab.id };
}

async function cmdReturnToProfile({ expectedProfileId = "" } = {}) {
  if (expectedProfileId && !/^[A-Za-z0-9_-]+$/.test(expectedProfileId)) {
    throw new Error("返回主页需要合法的 expectedProfileId");
  }
  const tab = await getOrOpenXhsTab();
  historyBackCount += 1;
  await chrome.tabs.goBack(tab.id);
  await waitForTabComplete(tab.id, null, 60000);
  const context = await readPageContext(tab.id);
  if (
    context.pageType !== "profile"
    || !context.profileId
    || (expectedProfileId && context.profileId !== expectedProfileId)
  ) {
    throw new Error("返回后不是预期用户主页；拒绝直链补偿");
  }
  if (context.riskSignals?.length) {
    throw new Error(`页面风险提示: ${context.riskSignals.join(",")}`);
  }
  return { ...context, boundTabId: tab.id };
}

async function cmdOpenSearchResult({ expectedNoteId = "" } = {}) {
  void expectedNoteId;
  throw new Error("legacy combined open_search_result is disabled");
}

async function cmdWaitForLoad({ timeout = 60000 }) {
  const tab = await getOrOpenXhsTab();
  await waitForTabComplete(tab.id, null, timeout);
  return null;
}

async function waitForTabComplete(tabId, expectedUrlPrefix, timeout) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + timeout;

    function listener(id, info, updatedTab) {
      if (id !== tabId) return;
      if (info.status !== "complete") return;
      if (expectedUrlPrefix && !updatedTab.url?.startsWith(expectedUrlPrefix.slice(0, 20))) return;
      chrome.tabs.onUpdated.removeListener(listener);
      resolve();
    }

    chrome.tabs.onUpdated.addListener(listener);

    // 轮询兜底：若事件在监听前已触发
    const poll = async () => {
      if (Date.now() > deadline) {
        chrome.tabs.onUpdated.removeListener(listener);
        reject(new Error("页面加载超时"));
        return;
      }
      const tab = await chrome.tabs.get(tabId).catch(() => null);
      if (tab && tab.status === "complete") {
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
        return;
      }
      setTimeout(poll, 400);
    };
    setTimeout(poll, 600);
  });
}

// ───────────────────────── 截图 ─────────────────────────

async function cmdScreenshot() {
  const tab = await getOrOpenXhsTab();
  const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
  return { data: dataUrl.split(",")[1] };
}

// ───────────────────────── MAIN world JS 执行 ─────────────────────────

async function cmdEvaluateInMainWorld(method, params) {
  if (method === "evaluate") assertReadOnlyExpression(params?.expression);
  const tab = await getOrOpenXhsTab();
  const results = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "MAIN",
    func: mainWorldExecutor,
    args: [method, params],
  });
  const r = results?.[0]?.result;
  if (r && typeof r === "object" && "__xhs_error" in r) {
    throw new Error(r.__xhs_error);
  }
  return r;
}

function assertReadOnlyExpression(expression) {
  if (typeof expression !== "string" || !expression.trim()) {
    throw new Error("evaluate requires a non-empty read-only expression");
  }
  if (expression.length > 100000) {
    throw new Error("evaluate read-only expression exceeds the size limit");
  }
  const forbidden = [
    /\.(?:click|submit|requestSubmit|reset|play|pause|select)\b/u,
    /\[\s*["'](?:click|submit|requestSubmit|reset|play|pause|select)["']\s*\]/u,
    /scrollIntoView\s*\(/u,
    /\bwindow\.scroll(?:By|To)\s*\(/u,
    /\.dispatchEvent\s*\(/u,
    /\bexecCommand\s*\(/u,
    /\.focus\s*\(/u,
    /\.blur\s*\(/u,
    /\.\s*[$A-Za-z_][\w$]*\s*=(?!=)/u,
    /\[[^\]]+\]\s*=(?!=)/u,
    /\.(?:value|checked|textContent|innerText|innerHTML)\s*=(?!=)/u,
    /\b(?:fetch|XMLHttpRequest|WebSocket)\b/u,
    /\.(?:setAttribute|removeAttribute|toggleAttribute|insertAdjacentText|insertAdjacentElement)\s*\(/u,
    /\b(?:remove|append|appendChild|prepend|insertBefore|replaceChild|replaceChildren|replaceWith|insertAdjacentHTML)\s*\(/u,
    /\.classList\.(?:add|remove|toggle|replace)\s*\(/u,
    /\.classList\s*\[\s*["'](?:add|remove|toggle|replace)["']\s*\]\s*\(/u,
    /\.style\.(?:setProperty|removeProperty)\s*\(/u,
    /\.(?:style|dataset)(?:\.[\w$-]+|\[[^\]]+\])\s*=(?!=)/u,
    /\b(?:localStorage|sessionStorage)\.(?:setItem|removeItem|clear)\s*\(/u,
    /\b(?:Object\.(?:assign|defineProperty|defineProperties|setPrototypeOf)|Reflect\.(?:set|defineProperty|deleteProperty|apply|construct))\s*\(/u,
    /\b(?:eval|Function|setTimeout|setInterval|queueMicrotask|requestAnimationFrame)\s*\(/u,
    /\bnew\s+Function\b/u,
    /\.(?:set|valueOf)\.call\s*\(/u,
    /\b(?:document\.cookie|window\.location|location\.href)\s*=(?!=)/u,
    /\b(?:window|document|location|history|navigator|globalThis)(?:\s*\.\s*[$\w]+|\s*\[[^\]]+\])*\s*=(?!=)/u,
    /\b(?:history\.(?:back|forward|go|pushState|replaceState)|location\.(?:assign|replace|reload))\s*\(/u,
    /\b(?:window\.open|document\.(?:open|close|write|writeln)|navigator\.sendBeacon)\s*\(/u,
    /\b(?:indexedDB|caches)\s*\./u,
    /\bdelete\s+/u,
    /(?:__proto__|\.prototype\s*[.=])/u,
    /\.(?:selectAllChildren|collapse|collapseToStart|collapseToEnd|setPosition|setBaseAndExtent)\s*\(/u,
    /\bnew\s+(?:Event|InputEvent|MouseEvent|WheelEvent)\s*\(/u,
  ];
  if (forbidden.some((pattern) => pattern.test(expression))) {
    throw new Error("evaluate is read-only; side-effect expression rejected");
  }
}

/**
 * 在页面主 world 运行，可访问 window.__INITIAL_STATE__ 等页面全局变量。
 * 注意：此函数被序列化后注入页面，不能引用外部变量。
 */
function mainWorldExecutor(method, params) {
  function poll(check, interval, timeout) {
    return new Promise((resolve, reject) => {
      const start = Date.now();
      (function tick() {
        const result = check();
        if (result !== false && result !== null && result !== undefined) {
          resolve(result);
          return;
        }
        if (Date.now() - start >= timeout) {
          reject(new Error("超时"));
          return;
        }
        setTimeout(tick, interval);
      })();
    });
  }

  switch (method) {
    case "evaluate": {
      try {
        // eslint-disable-next-line no-new-func
        return Function(`"use strict"; return (${params.expression})`)();
      } catch (e) {
        return { __xhs_error: `JS执行错误: ${e.message}` };
      }
    }

    case "has_element":
      return document.querySelector(params.selector) !== null;

    case "get_elements_count":
      return document.querySelectorAll(params.selector).length;

    case "get_element_text": {
      const el = document.querySelector(params.selector);
      return el ? el.textContent.trim() : null;
    }

    case "get_elements_info": {
      return Array.from(document.querySelectorAll(params.selector)).map(el => {
        const info = { text: el.textContent.trim() };
        if (params.attrs) for (const a of params.attrs) info[a] = el.getAttribute(a);
        return info;
      });
    }

    case "get_element_attribute": {
      const el = document.querySelector(params.selector);
      return el ? el.getAttribute(params.attr) : null;
    }

    case "get_scroll_top":
      return window.pageYOffset || document.documentElement.scrollTop || 0;

    case "get_viewport_height":
      return window.innerHeight;

    case "get_url":
      return window.location.href;

    case "wait_dom_stable": {
      const timeout = params.timeout || 10000;
      const interval = params.interval || 500;
      return new Promise((resolve) => {
        let last = -1;
        const start = Date.now();
        (function tick() {
          const size = document.body ? document.body.innerHTML.length : 0;
          if (size === last && size > 0) { resolve(null); return; }
          last = size;
          if (Date.now() - start >= timeout) { resolve(null); return; }
          setTimeout(tick, interval);
        })();
      });
    }

    case "wait_for_selector": {
      const timeout = params.timeout || 30000;
      return poll(
        () => document.querySelector(params.selector) ? true : false,
        200,
        timeout,
      ).catch(() => { throw new Error(`等待元素超时: ${params.selector}`); });
    }

    case "get_iframe_text": {
      return new Promise(resolve => {
        const iframe = document.querySelector(params.iframe_selector);
        if (!iframe) { resolve(null); return; }
        function tryRead() {
          try {
            const doc = iframe.contentDocument || iframe.contentWindow?.document;
            if (!doc || doc.readyState !== "complete") return false;
            const spans = doc.querySelectorAll(params.text_selector || ".textLayer span");
            if (spans.length === 0) return false;
            return Array.from(spans).map(s => s.textContent).join(" ");
          } catch (e) {
            return { __xhs_error: `iframe 访问失败: ${e.message}` };
          }
        }
        const timeout = params.timeout || 15000;
        const start = Date.now();
        (function tick() {
          const result = tryRead();
          if (result && result !== false) { resolve(result); return; }
          if (result?.__xhs_error) { resolve(result); return; }
          if (Date.now() - start >= timeout) { resolve(null); return; }
          setTimeout(tick, 300);
        })();
      });
    }

    default:
      return { __xhs_error: `未知 MAIN world 方法: ${method}` };
  }
}

// ───────────────────────── 工具函数 ──────────────────────────────────

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ───────────────────────── Audited browser pointer click ────────────

// Dispatch one visible pointer sequence at a verified viewport coordinate.
async function _dispatchVisibleClickAt(target, x, y) {
  // One explicit pointer move and click is sufficient for a visible action.
  // Do not manufacture randomized cursor paths as an anti-detection signal.
  await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
    type: "mouseMoved", x, y, button: "none", buttons: 0, modifiers: 0,
  });
  await sleep(50);

  const base = { x, y, button: "left", buttons: 1, clickCount: 1, modifiers: 0 };
  await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", { ...base, type: "mousePressed" });
  await sleep(50);
  await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", { ...base, type: "mouseReleased", buttons: 0 });
}

async function cmdClickViaDebugger(method, { selector, index, text }) {
  if (typeof selector !== "string" || !selector) throw new Error("click selector is required");
  if (method === "click_nth_element" && (!Number.isInteger(index) || index < 0)) {
    throw new Error("click index must be non-negative");
  }
  if (method === "click_element_by_text" && typeof text !== "string") {
    throw new Error("click text must be a string");
  }
  const tab = await getOrOpenXhsTab();
  const target = { tabId: tab.id };

  // 按调用方式构造元素查找表达式
  let findExpr;
  if (method === "click_nth_element") {
    findExpr = `document.querySelectorAll(${JSON.stringify(selector)})[${index}] || null`;
  } else if (method === "click_element_by_text") {
    findExpr = `Array.from(document.querySelectorAll(${JSON.stringify(selector)})).find(e => e.textContent.includes(${JSON.stringify(text)})) || null`;
  } else {
    findExpr = `document.querySelector(${JSON.stringify(selector)})`;
  }

  // 防御性 detach：清掉残留 attach 状态
  await chrome.debugger.detach(target).catch(() => {});
  await chrome.debugger.attach(target, "1.3");
  try {
    // Clicking never scrolls implicitly. The caller must first request one
    // bounded semantic-scroll step and then click this exact visible control.
    const evalResult = await chrome.debugger.sendCommand(target, "Runtime.evaluate", {
      expression: `(() => {
        const el = ${findExpr};
        if (!el) return null;
        const r = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        if (r.width <= 0 || r.height <= 0 || style.display === "none" || style.visibility === "hidden") return null;
        if (r.bottom <= 0 || r.right <= 0 || r.top >= innerHeight || r.left >= innerWidth) return null;
        const x = r.left + r.width / 2, y = r.top + r.height / 2;
        const hit = document.elementFromPoint(x, y);
        if (!hit || !(el === hit || el.contains(hit) || hit.contains(el))) return null;
        return { x, y };
      })()`,
      returnByValue: true,
    });

    const pos = evalResult?.result?.value;
    if (!pos) throw new Error(`元素不存在: ${JSON.stringify({ selector, index, text })}`);

    await _dispatchVisibleClickAt(target, pos.x, pos.y);
  } finally {
    await chrome.debugger.detach(target).catch(() => {});
  }
  return null;
}

// ─────── Audited browser key input ───────────────────────────────────

async function cmdPressKeyViaDebugger({ key }) {
  const tab = await getOrOpenXhsTab();
  const KEY_MAP = {
    Enter:      { key: "Enter",      code: "Enter",      windowsVirtualKeyCode: 13 },
    Tab:        { key: "Tab",        code: "Tab",        windowsVirtualKeyCode: 9  },
    Backspace:  { key: "Backspace",  code: "Backspace",  windowsVirtualKeyCode: 8  },
    Delete:     { key: "Delete",     code: "Delete",     windowsVirtualKeyCode: 46 },
    Escape:     { key: "Escape",     code: "Escape",     windowsVirtualKeyCode: 27 },
    ArrowDown:  { key: "ArrowDown",  code: "ArrowDown",  windowsVirtualKeyCode: 40 },
    ArrowUp:    { key: "ArrowUp",    code: "ArrowUp",    windowsVirtualKeyCode: 38 },
    ArrowLeft:  { key: "ArrowLeft",  code: "ArrowLeft",  windowsVirtualKeyCode: 37 },
    ArrowRight: { key: "ArrowRight", code: "ArrowRight", windowsVirtualKeyCode: 39 },
    Space:      { key: " ",          code: "Space",      windowsVirtualKeyCode: 32 },
  };
  const info = KEY_MAP[key] || { key, code: `Key${key.toUpperCase()}`, windowsVirtualKeyCode: key.charCodeAt(0) };

  const target = { tabId: tab.id };
  await chrome.debugger.detach(target).catch(() => {});
  await chrome.debugger.attach(target, "1.3");
  try {
    const base = { modifiers: 0, ...info };
    await chrome.debugger.sendCommand(target, "Input.dispatchKeyEvent", { ...base, type: "keyDown" });
    await sleep(30);
    await chrome.debugger.sendCommand(target, "Input.dispatchKeyEvent", { ...base, type: "keyUp" });
  } finally {
    await chrome.debugger.detach(target).catch(() => {});
  }
  return null;
}

// ─────── Audited progressive text input ──────────────────────────────

async function cmdTypeTextViaDebugger({ text, delayMs = 50 }) {
  if (typeof text !== "string" || !text) throw new Error("text is required");
  const tab = await getOrOpenXhsTab();
  const target = { tabId: tab.id };
  await chrome.debugger.detach(target).catch(() => {});
  await chrome.debugger.attach(target, "1.3");
  try {
    for (const char of text) {
      const key = char === "\n" ? "Enter" : char;
      const code = char === "\n" ? "Enter" : "";
      const windowsVirtualKeyCode = char === "\n" ? 13 : char.codePointAt(0);
      await chrome.debugger.sendCommand(target, "Input.dispatchKeyEvent", {
        type: "keyDown", key, code, windowsVirtualKeyCode, text: char, modifiers: 0,
      });
      await chrome.debugger.sendCommand(target, "Input.dispatchKeyEvent", {
        type: "keyUp", key, code, windowsVirtualKeyCode, modifiers: 0,
      });
      await sleep(Math.max(25, Math.round(delayMs)));
    }
  } finally {
    await chrome.debugger.detach(target).catch(() => {});
  }
  return null;
}

// ───────────────────────── 文件上传（chrome.debugger + CDP） ─────────

async function cmdInputContentEditableViaDebugger({ selector, index = 0, text }) {
  if (typeof selector !== "string" || !selector) throw new Error("selector is required");
  if (!Number.isInteger(index) || index < 0) throw new Error("editor index must be non-negative");
  if (typeof text !== "string" || !text || [...text].length > 2000) {
    throw new Error("progressive editor text must contain 1-2000 characters");
  }
  const tab = await getOrOpenXhsTab();
  const target = { tabId: tab.id };
  const trace = [];
  await chrome.debugger.detach(target).catch(() => {});
  await chrome.debugger.attach(target, "1.3");
  try {
    const located = await chrome.debugger.sendCommand(target, "Runtime.evaluate", {
      expression: `(() => {
        const el = document.querySelectorAll(${JSON.stringify(selector)})[${Number(index) || 0}];
        if (!el) return null;
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        if (rect.width <= 0 || rect.height <= 0 || style.display === "none" || style.visibility === "hidden") return null;
        if (rect.bottom <= 0 || rect.right <= 0 || rect.top >= innerHeight || rect.left >= innerWidth) return null;
        const x = rect.left + rect.width / 2, y = rect.top + rect.height / 2;
        const hit = document.elementFromPoint(x, y);
        if (!hit || !(el === hit || el.contains(hit) || hit.contains(el))) return null;
        return { x, y };
      })()`,
      returnByValue: true,
    });
    const pos = located?.result?.value;
    if (!pos) throw new Error(`visible editor does not exist: ${selector}`);

    await _dispatchVisibleClickAt(target, pos.x, pos.y);
    trace.push("focus_click");
    const keyBase = { modifiers: 2, key: "a", code: "KeyA", windowsVirtualKeyCode: 65 };
    await chrome.debugger.sendCommand(target, "Input.dispatchKeyEvent", { ...keyBase, type: "keyDown" });
    await chrome.debugger.sendCommand(target, "Input.dispatchKeyEvent", { ...keyBase, type: "keyUp" });
    await chrome.debugger.sendCommand(target, "Input.dispatchKeyEvent", {
      type: "keyDown", key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8, modifiers: 0,
    });
    await chrome.debugger.sendCommand(target, "Input.dispatchKeyEvent", {
      type: "keyUp", key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8, modifiers: 0,
    });
    trace.push("trusted_clear");

    let inserted = 0;
    for (const char of [...text]) {
      const key = char === "\n" ? "Enter" : char;
      const code = char === "\n" ? "Enter" : "";
      const windowsVirtualKeyCode = char === "\n" ? 13 : char.codePointAt(0);
      await chrome.debugger.sendCommand(target, "Input.dispatchKeyEvent", {
        type: "keyDown", key, code, windowsVirtualKeyCode, text: char, modifiers: 0,
      });
      await chrome.debugger.sendCommand(target, "Input.dispatchKeyEvent", {
        type: "keyUp", key, code, windowsVirtualKeyCode, modifiers: 0,
      });
      inserted += 1;
      await sleep(100);
      if (/[，。！？；,.!?;]/u.test(char)) await sleep(200);
    }
    trace.push("progressive_insert");

    const readback = await chrome.debugger.sendCommand(target, "Runtime.evaluate", {
      expression: `(() => {
        const el = document.querySelectorAll(${JSON.stringify(selector)})[${Number(index) || 0}];
        if (!el) return null;
        const raw = ("value" in el) ? String(el.value || "") : String(el.innerText || el.textContent || "");
        return raw.replace(/\\r\\n/g, "\\n");
      })()`,
      returnByValue: true,
    });
    const actual = readback?.result?.value;
    const verified = typeof actual === "string" && actual === text.replace(/\r\n/g, "\n");
    trace.push("exact_readback");
    if (!verified) throw new Error("progressive editor input failed exact readback");
    return { verified: true, characterCount: inserted, trace };
  } finally {
    await chrome.debugger.detach(target).catch(() => {});
  }
}

async function cmdPointerEventViaDebugger({ mode, x, y }) {
  if (!["move", "click"].includes(mode) || !Number.isFinite(x) || !Number.isFinite(y)) {
    throw new Error("invalid pointer event");
  }
  const tab = await getOrOpenXhsTab();
  const target = { tabId: tab.id };
  await chrome.debugger.detach(target).catch(() => {});
  await chrome.debugger.attach(target, "1.3");
  try {
    if (mode === "click") {
      await _dispatchVisibleClickAt(target, x, y);
    } else {
      await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
        type: "mouseMoved", x, y, button: "none", buttons: 0, modifiers: 0,
      });
    }
    return { completed: true, mode };
  } finally {
    await chrome.debugger.detach(target).catch(() => {});
  }
}

async function cmdSemanticScrollViaDebugger({ mode, x = 0, y = 0, selector = "", index = 0 }) {
  if (!["delta", "absolute", "bottom", "element", "nth_element"].includes(mode)) {
    throw new Error("invalid semantic scroll mode");
  }
  if ((mode === "element" || mode === "nth_element") && (typeof selector !== "string" || !selector)) {
    throw new Error("semantic element scroll requires selector");
  }
  if (mode === "nth_element" && (!Number.isInteger(index) || index < 0)) {
    throw new Error("semantic nth-element scroll requires a non-negative index");
  }
  const tab = await getOrOpenXhsTab();
  const target = { tabId: tab.id };
  await chrome.debugger.detach(target).catch(() => {});
  await chrome.debugger.attach(target, "1.3");
  try {
    const metrics = await chrome.debugger.sendCommand(target, "Runtime.evaluate", {
      expression: `(() => {
        const mode = ${JSON.stringify(mode)};
        const selector = ${JSON.stringify(selector)};
        const index = ${Number(index) || 0};
        const viewport = Math.max(320, window.innerHeight || 768);
        const currentX = window.scrollX || 0;
        const currentY = window.scrollY || 0;
        let dx = ${Number(x) || 0};
        let dy = ${Number(y) || 0};
        if (mode === "absolute") { dx -= currentX; dy -= currentY; }
        if (mode === "bottom") dy = Math.max(0, document.documentElement.scrollHeight - viewport - currentY);
        if (mode === "element" || mode === "nth_element") {
          const nodes = document.querySelectorAll(selector);
          const el = nodes[mode === "nth_element" ? index : 0];
          if (!el) return { found: false };
          const rect = el.getBoundingClientRect();
          dy = rect.top + rect.height / 2 - viewport / 2;
        }
        const limit = viewport * 1.1;
        return {
          found: true,
          dx: Math.max(-limit, Math.min(limit, dx)),
          dy: Math.max(-limit, Math.min(limit, dy)),
          cx: Math.max(1, (window.innerWidth || 1024) / 2),
          cy: Math.max(1, viewport / 2),
        };
      })()`,
      returnByValue: true,
    });
    const value = metrics?.result?.value;
    if (!value?.found) throw new Error("semantic scroll target does not exist");
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
      type: "mouseWheel", x: value.cx, y: value.cy,
      deltaX: value.dx, deltaY: value.dy, modifiers: 0,
    });
    return { completed: true, mode, deltaX: value.dx, deltaY: value.dy };
  } finally {
    await chrome.debugger.detach(target).catch(() => {});
  }
}

async function cmdSimulateReadingViaDebugger({ durationMs = 12000 }) {
  if (!Number.isInteger(durationMs) || durationMs < 10000 || durationMs > 15000) {
    throw new Error("reading duration must be 10000-15000 ms");
  }
  const tab = await getOrOpenXhsTab();
  const before = await readPageContext(tab.id);
  if (!before || !before.pageType || (before.riskSignals || []).length > 0) {
    throw new Error("passive observation requires a healthy identified page");
  }
  await sleep(durationMs);
  const after = await readPageContext(tab.id);
  if (!after || (after.riskSignals || []).length > 0) {
    throw new Error("passive observation ended on an unhealthy page");
  }
  const beforeIdentity = `${before.pageType}|${before.noteId || ""}|${before.pathname || ""}|${before.query || ""}`;
  const afterIdentity = `${after.pageType}|${after.noteId || ""}|${after.pathname || ""}|${after.query || ""}`;
  if (beforeIdentity !== afterIdentity) {
    throw new Error("page identity changed during passive observation");
  }
  return { completed: true, durationMs, eventCount: 0, trace: "bounded_passive_observation" };
}

async function cmdSetFileInputViaDebugger({ selector, files }) {
  const tab = await getOrOpenXhsTab();
  const target = { tabId: tab.id };

  await chrome.debugger.attach(target, "1.3");
  try {
    const { root } = await chrome.debugger.sendCommand(target, "DOM.getDocument", { depth: 0 });
    const { nodeId } = await chrome.debugger.sendCommand(target, "DOM.querySelector", {
      nodeId: root.nodeId,
      selector,
    });
    if (!nodeId) throw new Error(`文件输入框不存在: ${selector}`);
    await chrome.debugger.sendCommand(target, "DOM.setFileInputFiles", {
      nodeId,
      files,  // 本地文件路径数组，由 Python 侧提供
    });
  } finally {
    await chrome.debugger.detach(target).catch(() => {});
  }
  return null;
}

// ───────────────────────── 404 诊断事件存储 ──────────────────


// ───────────────────────── Tab 管理 ─────────────────────────

async function getOrOpenXhsTab() {
  if (boundXhsTabId !== null) {
    const bound = await chrome.tabs.get(boundXhsTabId).catch(() => null);
    if (isXhsTab(bound)) return bound;
    boundXhsTabId = null;
  }
  const activeTabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  const activeXhsTab = activeTabs.find(isXhsTab);
  if (activeXhsTab?.id && await isHealthyXhsTab(activeXhsTab)) {
    boundXhsTabId = activeXhsTab.id;
    return activeXhsTab;
  }
  const tabs = await queryAllXhsTabs();
  const healthyTab = await findHealthyRecentXhsTab(tabs);
  if (healthyTab?.id) {
    boundXhsTabId = healthyTab.id;
    return healthyTab;
  }
  if (tabs.length > 0) {
    throw new Error("已打开的小红书标签均不健康，拒绝自动新建或切换入口");
  }
  // 没有任何 XHS 页面时才新建首页。
  const tab = await chrome.tabs.create({ url: "https://www.xiaohongshu.com/" });
  await waitForTabComplete(tab.id, null, 30000);
  boundXhsTabId = tab.id;
  return tab;
}

async function queryAllXhsTabs() {
  return await chrome.tabs.query({
    url: [
      "https://www.xiaohongshu.com/*",
      "https://xiaohongshu.com/*",
      "https://creator.xiaohongshu.com/*",
    ],
  });
}

async function isHealthyXhsTab(tab) {
  if (!isXhsTab(tab) || !tab.id) return false;
  try {
    const context = await readPageContext(tab.id);
    return isHealthyContext(context);
  } catch {
    return false;
  }
}

async function findHealthyRecentXhsTab(tabs) {
  const ordered = sortTabsByRecency(tabs);
  for (const tab of ordered) {
    if (await isHealthyXhsTab(tab)) return tab;
  }
  return null;
}

function isXhsTab(tab) {
  return isXhsUrl(tab?.url || "");
}

chrome.tabs.onRemoved.addListener((tabId) => {
  if (tabId === boundXhsTabId) boundXhsTabId = null;
});

// ───────────────────────── 启动 ─────────────────────────

connect();
