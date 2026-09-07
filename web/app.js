(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));
  const SVG_NS = "http://www.w3.org/2000/svg";
  const ZOOMS = [1, 2, 4, 8, 16, 32, 48];

  const state = {
    online: false,
    checking: true,
    status: null,
    history: null,
    historyDays: 1,
    bucketSeconds: 1800,
    focus: "best",
    zoomIndex: 0,
    pan: 1,
    nodeFilter: "all",
    draftNodes: [],
    formHydrated: false,
    settingsDirty: false,
    action: "",
    pollTimer: null,
    toastTimer: null,
    historyTimer: null,
    historyKey: "",
    historyLoading: false,
    historyToken: 0,
    drag: null,
    connectionState: "checking",
    connectionMessage: "正在自动检测本机 Clash",
  };

  const THEME_KEY = "clash-node-monitor-theme";

  function applyTheme(theme, persist = true) {
    const next = theme === "light" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    const toggle = $("#themeToggle");
    const use = toggle?.querySelector("use");
    const label = toggle?.querySelector("span");
    if (use) use.setAttribute("href", next === "light" ? "#icon-moon" : "#icon-sun");
    if (label) label.textContent = next === "light" ? "夜间模式" : "白天模式";
    if (toggle) {
      const description = next === "light" ? "切换到夜间模式" : "切换到白天模式";
      toggle.title = description;
      toggle.setAttribute("aria-label", description);
    }
    const meta = $("#themeColorMeta");
    if (meta) meta.content = next === "light" ? "#f4f6f8" : "#11161c";
    if (persist) {
      try {
        window.localStorage.setItem(THEME_KEY, next);
      } catch {
        // The theme still applies when storage is unavailable.
      }
    }
  }

  function initTheme() {
    let saved = "";
    try {
      saved = window.localStorage.getItem(THEME_KEY) || "";
    } catch {
      saved = "";
    }
    const systemTheme = window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
    applyTheme(saved || systemTheme, false);
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function formatClock(timestamp) {
    if (!timestamp) return "—";
    return new Date(Number(timestamp) * 1000).toLocaleTimeString("zh-CN", {
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }

  function formatShortDate(timestamp) {
    if (!timestamp) return "—";
    return new Date(Number(timestamp) * 1000).toLocaleDateString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
    });
  }

  function formatDelay(value) {
    return Number.isFinite(Number(value)) ? `${Math.round(Number(value))} ms` : "—";
  }

  function formatCount(value) {
    return Number.isFinite(Number(value)) ? String(Math.max(0, Math.round(Number(value)))) : "—";
  }

  function formatInterval(seconds) {
    const value = Number(seconds);
    if (!Number.isFinite(value)) return "—";
    if (value < 60) return `${Math.round(value)} 秒`;
    if (value % 3600 === 0) return `${Math.round(value / 3600)} 小时`;
    return `${Math.round(value / 60)} 分钟`;
  }

  function naturalCompare(a, b) {
    return String(a).localeCompare(String(b), "zh-CN", { numeric: true, sensitivity: "base" });
  }

  function dateKey(offset = 0) {
    const value = new Date();
    value.setHours(0, 0, 0, 0);
    value.setDate(value.getDate() - offset);
    const year = value.getFullYear();
    const month = String(value.getMonth() + 1).padStart(2, "0");
    const day = String(value.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  function historyDates(days) {
    return Array.from({ length: Math.max(1, days) }, (_, index) => dateKey(index)).reverse();
  }

  function effectiveBucket() {
    if (state.bucketSeconds > 0) return state.bucketSeconds;
    return Math.max(15, Number(state.status?.settings?.intervalSeconds || 60));
  }

  function statusState(row, warningDelay) {
    if (!row) return "neutral";
    if (row.status === "timeout") return "timeout";
    if (row.status === "error") return "error";
    if (row.status === "ok" && Number(row.delayMs) <= Number(warningDelay)) return "ok";
    if (row.status === "ok") return "warn";
    return "neutral";
  }

  function statusLabel(row, warningDelay) {
    if (!row) return "未采样";
    if (row.status === "timeout") return "Timeout";
    if (row.status === "error") return "失败";
    if (row.status === "ok" && Number(row.delayMs) > Number(warningDelay)) return "偏高";
    if (row.status === "ok") return "正常";
    return "未采样";
  }

  function isGreen(row, warningDelay) {
    return Boolean(row && row.status === "ok" && Number.isFinite(Number(row.delayMs)) && Number(row.delayMs) <= Number(warningDelay));
  }

  function currentSettings() {
    return state.status?.settings || {};
  }

  function latestRows() {
    return Array.isArray(state.status?.latest) ? state.status.latest : [];
  }

  function latestMap() {
    return new Map(latestRows().map((row) => [String(row.node), row]));
  }

  function availableNodes() {
    const settings = currentSettings();
    const fromApi = Array.isArray(state.status?.availableNodes) ? state.status.availableNodes : [];
    const fromSettings = Array.isArray(settings.selectedNodes) ? settings.selectedNodes : [];
    const fallback = fromApi.length || fromSettings.length ? [] : latestRows().map((row) => row.node).filter(Boolean);
    return Array.from(new Set([...fromApi, ...fromSettings, ...fallback])).sort(naturalCompare);
  }

  function allHistoryNodes() {
    const series = state.history?.series || {};
    return Object.keys(series).sort(naturalCompare);
  }

  async function fetchJson(path, options = {}, timeoutMs = 5000) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(path, { cache: "no-store", ...options, signal: controller.signal });
      const text = await response.text();
      let payload = {};
      try {
        payload = text ? JSON.parse(text) : {};
      } catch {
        payload = { ok: false, message: "服务返回了无法读取的内容" };
      }
      if (!response.ok || payload.ok === false) {
        const error = new Error(payload.message || `请求失败（${response.status}）`);
        error.payload = payload;
        error.status = response.status;
        throw error;
      }
      return payload;
    } finally {
      window.clearTimeout(timer);
    }
  }

  async function postJson(path, payload) {
    return fetchJson(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    });
  }

  async function downloadCsv() {
    const response = await fetch(`/api/export?days=${state.historyDays}`, { cache: "no-store" });
    if (!response.ok) {
      let message = `导出失败（${response.status}）`;
      try {
        const payload = await response.json();
        message = payload.message || message;
      } catch {
        // Keep the status-based message when the server did not return JSON.
      }
      throw new Error(message);
    }
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const filename = disposition.match(/filename="([^"]+)"/)?.[1] || `clash-node-monitor-${state.historyDays}d.csv`;
    const rowCount = Number(response.headers.get("X-Export-Row-Count") || 0);
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    showToast(rowCount ? `已导出 ${rowCount} 条原始采样` : "当前范围没有采样，已导出空表头", rowCount ? "success" : "warning");
  }

  function showToast(message, kind = "info") {
    const toast = $("#toast");
    if (!toast) return;
    window.clearTimeout(state.toastTimer);
    toast.textContent = message;
    toast.dataset.kind = kind;
    toast.hidden = false;
    state.toastTimer = window.setTimeout(() => {
      toast.hidden = true;
    }, 3600);
  }

  function setNote(message, kind = "loading") {
    const note = $("#chartNote");
    const mark = document.querySelector(".note-mark");
    if (note) note.textContent = message;
    if (mark) mark.dataset.state = kind;
  }

  function hydrateSettings(settings = {}, force = false) {
    if (state.settingsDirty && !force) return;
    const mapping = {
      "#intervalInput": settings.intervalSeconds,
      "#timeoutInput": settings.timeoutMs,
      "#controllerInput": settings.controller,
      "#patternInput": settings.nodePattern,
      "#testUrlInput": settings.testUrl,
      "#autoRouteInput": Boolean(settings.autoRoute),
      "#routeGroupInput": settings.routeGroup,
      "#routeAfterInput": settings.routeAfterFailures,
      "#routeCooldownInput": settings.routeCooldownSeconds,
      "#warningDelayInput": settings.warningDelayMs,
    };
    Object.entries(mapping).forEach(([selector, value]) => {
      const element = $(selector);
      if (!element || value === undefined || value === null) return;
      if (element.type === "checkbox") element.checked = Boolean(value);
      else element.value = String(value);
    });
    state.draftNodes = Array.isArray(settings.selectedNodes) ? [...settings.selectedNodes] : [];
    state.formHydrated = true;
    const secretHint = $("#secretHint");
    if (secretHint) secretHint.textContent = settings.hasSecret ? "已保存本机密钥；留空会保持不变。" : "未检测到密钥；如果 Clash 设置了密钥，请在这里填写。";
    renderNodePicker();
  }

  function renderNodePicker() {
    const target = $("#nodePickerList");
    if (!target) return;
    const nodes = availableNodes();
    if (!nodes.length) {
      target.innerHTML = '<span class="muted-copy">打开服务后读取节点清单。</span>';
      return;
    }
    const selected = new Set(state.draftNodes);
    target.innerHTML = nodes.map((node) => `
      <label class="node-choice">
        <input type="checkbox" value="${escapeHtml(node)}" ${selected.has(node) ? "checked" : ""} />
        <span>${escapeHtml(node)}</span>
      </label>`).join("");
    target.querySelectorAll("input").forEach((input) => {
      input.addEventListener("change", () => {
        state.draftNodes = Array.from(target.querySelectorAll("input:checked")).map((item) => item.value);
        markFormDirty();
      });
    });
  }

  function markFormDirty() {
    state.settingsDirty = true;
    const status = $("#settingsStatus");
    if (status) status.textContent = "有未保存修改";
  }

  function readSettingsForm() {
    const number = (selector, fallback) => {
      const value = Number($(selector)?.value);
      return Number.isFinite(value) ? value : fallback;
    };
    return {
      intervalSeconds: number("#intervalInput", 60),
      timeoutMs: number("#timeoutInput", 5000),
      controller: $("#controllerInput")?.value.trim() || "",
      secret: $("#secretInput")?.value.trim() || "",
      clearSecret: Boolean($("#clearSecretInput")?.checked),
      nodePattern: $("#patternInput")?.value.trim() || ".*",
      testUrl: $("#testUrlInput")?.value.trim() || "",
      autoRoute: Boolean($("#autoRouteInput")?.checked),
      routeGroup: $("#routeGroupInput")?.value.trim() || "",
      routeAfterFailures: number("#routeAfterInput", 2),
      routeCooldownSeconds: number("#routeCooldownInput", 300),
      warningDelayMs: number("#warningDelayInput", 800),
      selectedNodes: [...state.draftNodes],
    };
  }

  function renderFocusOptions() {
    const select = $("#chartFocus");
    if (!select) return;
    const options = [{ value: "best", label: "最佳节点" }, ...allHistoryNodes().map((node) => ({ value: node, label: node }))];
    const current = state.focus;
    select.innerHTML = options.map((option) => `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)}</option>`).join("");
    state.focus = options.some((option) => option.value === current) ? current : "best";
    select.value = state.focus;
  }

  function mergeHistory(payloads) {
    const merged = { series: {}, startAt: 0, endAt: 0, bucketSeconds: effectiveBucket(), intervalSeconds: currentSettings().intervalSeconds };
    payloads.forEach((payload) => {
      if (!payload || payload.ok === false) return;
      merged.startAt = merged.startAt ? Math.min(merged.startAt, Number(payload.startAt || 0)) : Number(payload.startAt || 0);
      merged.endAt = Math.max(merged.endAt, Number(payload.endAt || 0));
      merged.bucketSeconds = Number(payload.bucketSeconds || merged.bucketSeconds);
      Object.entries(payload.series || {}).forEach(([node, points]) => {
        if (!Array.isArray(points)) return;
        merged.series[node] = [...(merged.series[node] || []), ...points];
      });
    });
    Object.values(merged.series).forEach((points) => {
      points.sort((a, b) => Number(a.timestamp) - Number(b.timestamp));
    });
    return merged;
  }

  async function loadHistory(force = false) {
    if (!state.online || state.historyLoading) return;
    const key = `${state.historyDays}:${effectiveBucket()}`;
    if (!force && state.historyKey === key && state.history) return;
    state.historyKey = key;
    state.historyLoading = true;
    setNote("正在读取历史数据", "loading");
    const token = ++state.historyToken;
    try {
      const payloads = await Promise.all(historyDates(state.historyDays).map((day) => fetchJson(`/api/history?date=${encodeURIComponent(day)}&bucketSeconds=${effectiveBucket()}`, {}, 7000)));
      if (token !== state.historyToken) return;
      state.history = mergeHistory(payloads);
      renderFocusOptions();
      renderChart();
    } catch (error) {
      if (token !== state.historyToken) return;
      state.history = null;
      setNote(error.message || "历史数据暂不可用", "error");
      renderChart();
    } finally {
      state.historyLoading = false;
    }
  }

  function maybeLoadHistory(force = false) {
    if (!state.online) return;
    if (force || !state.history || Date.now() - Number(state.history.loadedAt || 0) > 30000) {
      void loadHistory(true).then(() => {
        if (state.history) state.history.loadedAt = Date.now();
      });
    }
  }

  async function poll() {
    try {
      const payload = await fetchJson("/api/status", {}, 3500);
      state.status = payload;
      state.online = true;
      state.checking = false;
      if (!state.formHydrated) hydrateSettings(payload.settings || {});
      render();
      maybeLoadHistory();
    } catch (error) {
      state.online = false;
      state.checking = false;
      state.status = state.status || null;
      render();
      if (state.historyLoading) state.historyToken += 1;
    } finally {
      window.clearTimeout(state.pollTimer);
      const monitor = state.status?.monitor;
      const next = !state.online ? 8000 : monitor?.inCycle ? 1700 : 5000;
      state.pollTimer = window.setTimeout(poll, next);
    }
  }

  function render() {
    renderServiceState();
    renderConnectionState();
    renderOverview();
    renderRoute();
    renderControls();
    renderNodeTable();
    renderNodePicker();
    renderChart();
    renderOfflineState();
  }

  function renderServiceState() {
    const dot = $(".status-dot");
    const label = $("#railServiceStatus");
    if (dot) dot.dataset.state = state.checking ? "loading" : state.online ? (state.status?.monitor?.inCycle ? "sampling" : state.status?.monitor?.paused ? "paused" : "online") : "offline";
    if (label) label.textContent = state.checking ? "连接中" : state.online ? "在线" : "离线";
  }

  function renderConnectionState() {
    const card = $("#connectionCard");
    const label = $("#connectionStatusLabel");
    const detail = $("#connectionDetail");
    if (!card || !label || !detail) return;
    const settings = currentSettings();
    const controller = settings.controller || "自动检测";
    const nodeCount = availableNodes().length;
    const lastCycle = state.status?.lastCycle;
    const hasFailure = lastCycle?.status === "error" && !nodeCount;
    if (state.checking) {
      state.connectionState = "checking";
      state.connectionMessage = "正在自动检测本机 Clash";
    } else if (!state.online) {
      state.connectionState = "error";
      state.connectionMessage = "本机监控服务未运行";
    } else if (state.connectionState === "checking" && nodeCount) {
      state.connectionState = "ready";
      state.connectionMessage = `已发现 ${nodeCount} 个可用节点`;
    } else if (hasFailure && state.connectionState !== "ready") {
      state.connectionState = "error";
      state.connectionMessage = lastCycle.message || "无法读取 Clash 节点";
    }
    card.dataset.state = state.connectionState;
    label.textContent = state.connectionState === "ready" ? "Clash 已连接" : state.connectionState === "error" ? "Clash 尚未连接" : "正在自动检测 Clash";
    detail.textContent = state.connectionState === "ready" ? `${controller} · ${state.connectionMessage}` : `${state.connectionMessage}；可展开高级设置后重新测试。`;
  }

  function renderOverview() {
    const monitor = state.status?.monitor || {};
    const settings = currentSettings();
    const latest = latestRows();
    const warning = Number(settings.warningDelayMs || 800);
    const okRows = latest.filter((row) => row.status === "ok");
    const attention = latest.filter((row) => !isGreen(row, warning));
    const total = latest.length || availableNodes().length;
    const availability = total ? Math.round((okRows.length / total) * 100) : null;
    const averageValues = okRows.map((row) => Number(row.delayMs)).filter(Number.isFinite);
    const average = averageValues.length ? Math.round(averageValues.reduce((sum, value) => sum + value, 0) / averageValues.length) : null;
    const currentNode = String(state.status?.routing?.currentNode || "");
    const current = latest.find((row) => row.node === currentNode);
    const progress = monitor.inCycle && monitor.cycleProgress ? `采样中 · ${monitor.cycleProgress.completed || 0} / ${monitor.cycleProgress.total || total || "—"}` : "最近完整采样";

    $("#healthyCount").textContent = total ? String(okRows.length) : "—";
    $("#totalCount").textContent = total ? String(total) : "—";
    $("#availabilityLabel").textContent = availability === null ? "—" : `${availability}% 可用`;
    $("#healthCaption").textContent = state.online ? (monitor.paused ? "监控已暂停" : monitor.inCycle ? "本轮采样进行中" : "当前快照") : "等待服务";
    $("#cycleLabel").textContent = progress;
    $("#averageDelay").textContent = average === null ? "—" : formatDelay(average);
    $("#attentionCount").textContent = total ? String(attention.length) : "—";
    $("#attentionCaption").textContent = attention.length ? "Timeout / 超过绿色上限" : "没有需要关注的节点";
    $("#summaryCurrentNode").textContent = currentNode || "—";
    $("#summaryCurrentState").textContent = current ? `${statusLabel(current, warning)} · ${formatDelay(current.delayMs)}` : state.online ? "等待回读" : "服务离线";
    $("#summaryCurrentState").dataset.state = current ? statusState(current, warning) : "neutral";
    $("#topbarMode").textContent = monitor.inCycle ? progress : monitor.paused ? "已暂停采样" : monitor.nextRefreshSeconds ? `下轮采样 · ${formatInterval(monitor.nextRefreshSeconds)}` : "等待本轮完整采样";
    const last = state.status?.lastCycle?.finishedAt;
    $("#lastRefresh").textContent = last ? formatClock(last) : "—";

    const track = $("#healthTrack");
    if (track) {
      track.innerHTML = latest.length ? latest.map((row) => `<span data-state="${escapeHtml(statusState(row, warning))}" title="${escapeHtml(row.node)}"></span>`).join("") : "";
    }
  }

  function renderRoute() {
    const routing = state.status?.routing || {};
    const settings = currentSettings();
    const warning = Number(settings.warningDelayMs || 800);
    const currentNode = String(routing.currentNode || "");
    const current = latestRows().find((row) => row.node === currentNode);
    const enabled = Boolean(routing.enabled);
    const routeStrip = document.querySelector(".route-strip");
    if (routeStrip) routeStrip.dataset.route = enabled ? "enabled" : "disabled";
    $("#routeGroup").textContent = routing.group || routing.requestedGroup || settings.routeGroup || "未配置控制组";
    $("#currentNode").textContent = currentNode || "无法回读";
    const currentState = $("#currentNodeState");
    currentState.textContent = current ? `${statusLabel(current, warning)} · ${formatDelay(current.delayMs)}` : routing.lastMessage || "等待回读";
    currentState.dataset.state = current ? statusState(current, warning) : "neutral";
    $("#routeDecision").textContent = routeDecision(routing, state.status?.lastCycle);
    const toggle = $("#routeToggle");
    if (toggle) {
      toggle.setAttribute("aria-checked", String(enabled));
      toggle.disabled = !state.online || Boolean(state.action);
    }
    $("#routeToggleLabel").textContent = enabled ? "自动路由已开启" : "开启自动路由";
  }

  function routeDecision(routing, cycle) {
    if (routing.switching) return "正在向 Clash 写入新的选择…";
    if (cycle?.routeAction === "switched") {
      return `已切换：${cycle.routeFrom || "—"} → ${cycle.routeTo || "—"}`;
    }
    if (routing.lastMessage) return routing.lastMessage;
    return routing.enabled ? "自动路由已开启，等待下一轮判定" : "自动路由关闭，只监控和记录";
  }

  function renderControls() {
    const monitor = state.status?.monitor || {};
    const busy = Boolean(state.action);
    const online = state.online;
    const refresh = $("#refreshButton");
    const pause = $("#pauseButton");
    const stop = $("#stopButton");
    if (refresh) refresh.disabled = !online || Boolean(monitor.inCycle) || busy;
    if (pause) {
      pause.disabled = !online || busy;
      pause.querySelector("span")?.replaceChildren(document.createTextNode(monitor.paused ? "继续监控" : "暂停监控"));
      const use = pause.querySelector("use");
      if (use) use.setAttribute("href", monitor.paused ? "#icon-play" : "#icon-pause");
    }
    if (stop) stop.disabled = !online || busy;
    $("#loadConfigButton").disabled = !online || busy;
    $("#exportButton").disabled = !online || busy;
    $("#settingsStatus").textContent = state.settingsDirty ? "有未保存修改" : state.action === "save" ? "正在保存…" : "未修改";
    $("#autoRouteInput").disabled = !online || busy;
    $("#connectionTestButton").disabled = !online || busy;
  }

  function renderNodeTable() {
    const body = $("#nodeTableBody");
    if (!body) return;
    const settings = currentSettings();
    const warning = Number(settings.warningDelayMs || 800);
    const map = latestMap();
    const currentNode = String(state.status?.routing?.currentNode || "");
    let nodes = availableNodes();
    if (!nodes.length && !latestRows().length) {
      body.innerHTML = '<tr><td colspan="5" class="table-empty">首轮采样完成后显示每个节点的结果。</td></tr>';
      $("#nodesSubtitle").textContent = state.online ? "等待节点清单" : "服务离线";
      return;
    }
    nodes = nodes.filter((node) => {
      const row = map.get(node);
      if (state.nodeFilter === "attention") return !isGreen(row, warning);
      if (state.nodeFilter === "current") return node === currentNode;
      return true;
    });
    $("#nodesSubtitle").textContent = `${availableNodes().length} 个候选 · ${latestRows().length ? `最后一轮 ${formatClock(state.status?.lastCycle?.finishedAt)}` : "等待首轮采样"}`;
    if (!nodes.length) {
      body.innerHTML = '<tr><td colspan="5" class="table-empty">当前筛选没有匹配节点。</td></tr>';
      return;
    }
    body.innerHTML = nodes.map((node) => {
      const row = map.get(node);
      const stateName = statusState(row, warning);
      const label = statusLabel(row, warning);
      const delay = row?.delayMs === null || row?.delayMs === undefined ? "—" : formatDelay(row.delayMs);
      let note = row?.error || "尚未采样";
      if (row?.status === "ok" && Number.isFinite(Number(row.delayMs))) {
        note = isGreen(row, warning) ? `距绿色上限 ${Math.max(0, warning - Number(row.delayMs))} ms` : `超过绿色上限 ${Number(row.delayMs) - warning} ms`;
      }
      const currentMark = node === currentNode ? '<span class="current-badge">当前</span>' : "";
      return `<tr><td><span class="node-name">${escapeHtml(node)}</span>${currentMark}</td><td><span class="node-status" data-state="${escapeHtml(stateName)}">${escapeHtml(label)}</span></td><td><span class="node-delay" data-state="${escapeHtml(stateName)}">${escapeHtml(delay)}</span></td><td>${escapeHtml(row ? formatClock(row.sampledAt) : "—")}</td><td title="${escapeHtml(note)}">${escapeHtml(note)}</td></tr>`;
    }).join("");
  }

  function renderOfflineState() {
    const offline = $("#offlineState");
    if (!offline) return;
    offline.hidden = state.online || state.checking;
    $("#offlineMessage").textContent = state.checking ? "正在连接本机监控服务…" : "请先运行 run_monitor.bat，再回到此页面。";
  }

  function svgElement(name, attrs = {}, text = "") {
    const element = document.createElementNS(SVG_NS, name);
    Object.entries(attrs).forEach(([key, value]) => element.setAttribute(key, String(value)));
    if (text) element.textContent = text;
    return element;
  }

  function bestSeries(series) {
    const byTimestamp = new Map();
    Object.values(series || {}).forEach((points) => {
      (points || []).forEach((point) => {
        const timestamp = Number(point.timestamp);
        if (!Number.isFinite(timestamp)) return;
        const candidates = byTimestamp.get(timestamp) || [];
        candidates.push(point);
        byTimestamp.set(timestamp, candidates);
      });
    });
    return Array.from(byTimestamp.entries()).sort((a, b) => a[0] - b[0]).map(([timestamp, points]) => {
      const usable = points.filter((point) => Number.isFinite(Number(point.delayMs)) && ["ok", "degraded"].includes(point.status)).sort((a, b) => Number(a.delayMs) - Number(b.delayMs));
      if (usable.length) return { ...usable[0], timestamp };
      return { ...points[0], timestamp, delayMs: null };
    });
  }

  function chartSeries() {
    const series = state.history?.series || {};
    if (state.focus === "best") return [{ node: "最佳节点", points: bestSeries(series), best: true }];
    return [{ node: state.focus, points: Array.isArray(series[state.focus]) ? series[state.focus] : [], best: false }];
  }

  function renderChart() {
    const svg = $("#trendChart");
    const empty = $("#chartEmpty");
    if (!svg) return;
    svg.innerHTML = "";
    const series = chartSeries();
    const points = series.flatMap((item) => item.points || []).filter((point) => Number.isFinite(Number(point.timestamp)));
    if (!state.online || !points.length) {
      if (empty) empty.hidden = false;
      updateZoomLabel();
      if (!state.online) setNote("服务离线，趋势暂不可用", "error");
      else if (!state.historyLoading) setNote("还没有可显示的采样", "warning");
      return;
    }
    if (empty) empty.hidden = true;
    const allTimestamps = points.map((point) => Number(point.timestamp));
    const fullStart = Math.min(...allTimestamps);
    const fullEnd = Math.max(...allTimestamps, fullStart + effectiveBucket());
    const fullSpan = Math.max(effectiveBucket(), fullEnd - fullStart);
    const zoom = ZOOMS[state.zoomIndex];
    const visibleSpan = Math.max(effectiveBucket(), fullSpan / zoom);
    const maxStart = Math.max(fullStart, fullEnd - visibleSpan);
    const visibleStart = clamp(fullStart + (maxStart - fullStart) * state.pan, fullStart, maxStart);
    const visibleEnd = visibleStart + visibleSpan;
    const visiblePoints = points.filter((point) => Number(point.timestamp) >= visibleStart - effectiveBucket() && Number(point.timestamp) <= visibleEnd + effectiveBucket());
    const warning = Number(currentSettings().warningDelayMs || 800);
    const delays = visiblePoints.map((point) => Number(point.delayMs)).filter(Number.isFinite);
    const highest = Math.max(warning, ...delays, 300);
    const chartMax = Math.ceil((highest * 1.18) / 100) * 100;
    const left = 64;
    const right = 20;
    const top = 18;
    const bottom = 42;
    const width = 1100;
    const height = 360;
    const plotWidth = width - left - right;
    const plotHeight = height - top - bottom;
    const x = (timestamp) => left + ((Number(timestamp) - visibleStart) / Math.max(1, visibleSpan)) * plotWidth;
    const y = (delay) => top + (1 - clamp(Number(delay) / chartMax, 0, 1)) * plotHeight;

    const grid = svgElement("g", { class: "chart-grid" });
    for (let index = 0; index <= 3; index += 1) {
      const value = (chartMax / 3) * index;
      const yValue = y(value);
      grid.appendChild(svgElement("line", { x1: left, y1: yValue, x2: width - right, y2: yValue }));
      const axis = svgElement("g", { class: "chart-axis" });
      axis.appendChild(svgElement("text", { x: left - 10, y: yValue + 4, "text-anchor": "end" }, value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${Math.round(value)}ms`));
      grid.appendChild(axis);
    }
    svg.appendChild(grid);

    const thresholdY = y(warning);
    svg.appendChild(svgElement("line", { class: "chart-threshold", x1: left, y1: thresholdY, x2: width - right, y2: thresholdY }));
    svg.appendChild(svgElement("text", { class: "chart-threshold-label", x: width - right, y: thresholdY - 7, "text-anchor": "end" }, `绿色上限 ${warning} ms`));

    const timeAxis = svgElement("g", { class: "chart-time-axis" });
    const tickCount = 5;
    for (let index = 0; index <= tickCount; index += 1) {
      const timestamp = visibleStart + (visibleSpan / tickCount) * index;
      const xValue = x(timestamp);
      timeAxis.appendChild(svgElement("text", { x: xValue, y: height - 13, "text-anchor": index === 0 ? "start" : index === tickCount ? "end" : "middle" }, formatChartTime(timestamp, visibleSpan)));
    }
    svg.appendChild(timeAxis);

    series.forEach((item) => {
      const itemPoints = (item.points || []).filter((point) => Number(point.timestamp) >= visibleStart - effectiveBucket() && Number(point.timestamp) <= visibleEnd + effectiveBucket());
      const usable = itemPoints.filter((point) => Number.isFinite(Number(point.delayMs)));
      if (!usable.length) {
        itemPoints.filter((point) => !Number.isFinite(Number(point.delayMs))).forEach((point) => appendChartFailure(svg, point, x, y, item));
        return;
      }
      const path = usable.map((point, index) => `${index ? "L" : "M"}${x(point.timestamp).toFixed(2)},${y(point.delayMs).toFixed(2)}`).join(" ");
      if (item.best && usable.length > 1) {
        const area = `${path} L${x(usable[usable.length - 1].timestamp).toFixed(2)},${y(0).toFixed(2)} L${x(usable[0].timestamp).toFixed(2)},${y(0).toFixed(2)} Z`;
        svg.appendChild(svgElement("path", { class: "chart-area", d: area }));
      }
      svg.appendChild(svgElement("path", { class: `chart-series${item.best ? "" : " is-node"}`, d: path }));
      itemPoints.forEach((point) => {
        if (Number.isFinite(Number(point.delayMs))) appendChartPoint(svg, point, x, y, item);
        else appendChartFailure(svg, point, x, y, item);
      });
    });
    updateZoomLabel();
    const count = points.length;
    setNote(`${count} 个时间点 · ${formatInterval(effectiveBucket())} 聚合 · ${state.historyDays} 天范围`, "ready");
  }

  function appendChartPoint(svg, point, x, y, item) {
    const circle = svgElement("circle", { class: `chart-point${item.best ? "" : " is-node"}`, cx: x(point.timestamp), cy: y(point.delayMs), r: 3.4 });
    circle.dataset.chartPoint = "1";
    circle.dataset.node = item.node;
    circle.dataset.timestamp = String(point.timestamp);
    circle.dataset.status = point.status || "ok";
    circle.dataset.delay = String(point.delayMs);
    circle.appendChild(svgElement("title", {}, `${item.node} · ${formatDelay(point.delayMs)}`));
    svg.appendChild(circle);
  }

  function appendChartFailure(svg, point, x, y, item) {
    const circle = svgElement("circle", { class: "chart-failure", cx: x(point.timestamp), cy: y(0), r: 4.4 });
    circle.dataset.chartPoint = "1";
    circle.dataset.node = item.node;
    circle.dataset.timestamp = String(point.timestamp);
    circle.dataset.status = point.status || "error";
    circle.dataset.delay = "";
    circle.appendChild(svgElement("title", {}, `${item.node} · ${point.status === "timeout" ? "Timeout" : "失败"}`));
    svg.appendChild(circle);
  }

  function formatChartTime(timestamp, span) {
    const date = new Date(Number(timestamp) * 1000);
    if (span > 3 * 86400) return `${date.getMonth() + 1}/${date.getDate()}`;
    return date.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" });
  }

  function updateZoomLabel() {
    const label = $("#zoomLabel");
    if (!label) return;
    const zoom = ZOOMS[state.zoomIndex];
    label.textContent = zoom === 1 ? "1× · 全时段" : `${zoom}× · 可拖动`;
    $("#zoomOut").disabled = state.zoomIndex <= 0;
    $("#zoomIn").disabled = state.zoomIndex >= ZOOMS.length - 1;
  }

  function showChartTooltip(pointElement) {
    const tooltip = $("#chartTooltip");
    const wrap = $("#chartWrap");
    if (!tooltip || !wrap || !pointElement) return;
    const node = pointElement.dataset.node || "节点";
    const status = pointElement.dataset.status || "error";
    const delay = pointElement.dataset.delay ? formatDelay(pointElement.dataset.delay) : status === "timeout" ? "Timeout" : "失败";
    tooltip.innerHTML = `<div class="chart-tooltip-head"><strong>${escapeHtml(node)}</strong><time>${escapeHtml(formatClock(pointElement.dataset.timestamp))}</time></div><div class="chart-tooltip-value"><strong>${escapeHtml(delay)}</strong><span>${escapeHtml(status === "ok" ? "正常" : status === "timeout" ? "Timeout" : "不可用")}</span></div>`;
    const pointRect = pointElement.getBoundingClientRect();
    const wrapRect = wrap.getBoundingClientRect();
    tooltip.hidden = false;
    const left = clamp(pointRect.left - wrapRect.left + 8, 8, Math.max(8, wrapRect.width - tooltip.offsetWidth - 8));
    const top = clamp(pointRect.top - wrapRect.top - tooltip.offsetHeight - 8, 8, Math.max(8, wrapRect.height - tooltip.offsetHeight - 8));
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
  }

  function bindEvents() {
    $("#themeToggle")?.addEventListener("click", () => {
      applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
    });

    $$("[data-history-days]").forEach((button) => button.addEventListener("click", () => {
      state.historyDays = Number(button.dataset.historyDays) || 1;
      state.history = null;
      state.historyKey = "";
      $$("[data-history-days]").forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
      $("#trend-title").textContent = state.historyDays === 1 ? "24 小时趋势" : `${state.historyDays} 天趋势`;
      maybeLoadHistory(true);
      renderChart();
    }));

    $("#chartFocus")?.addEventListener("change", (event) => {
      state.focus = event.target.value;
      renderChart();
    });
    $("#chartBucket")?.addEventListener("change", (event) => {
      state.bucketSeconds = Number(event.target.value);
      state.history = null;
      state.historyKey = "";
      maybeLoadHistory(true);
    });
    $("#zoomOut")?.addEventListener("click", () => { state.zoomIndex = clamp(state.zoomIndex - 1, 0, ZOOMS.length - 1); renderChart(); });
    $("#zoomIn")?.addEventListener("click", () => { state.zoomIndex = clamp(state.zoomIndex + 1, 0, ZOOMS.length - 1); state.pan = 1; renderChart(); });
    $("#zoomReset")?.addEventListener("click", () => { state.zoomIndex = 0; state.pan = 1; renderChart(); });

    const chart = $("#trendChart");
    chart?.addEventListener("pointerover", (event) => {
      const target = event.target?.closest?.("[data-chart-point]");
      if (target) showChartTooltip(target);
    });
    chart?.addEventListener("pointerout", (event) => {
      if (!event.relatedTarget?.closest?.("[data-chart-point]")) $("#chartTooltip").hidden = true;
    });
    chart?.addEventListener("pointerdown", (event) => {
      if (state.zoomIndex <= 0) return;
      state.drag = { x: event.clientX, pan: state.pan };
      chart.setPointerCapture?.(event.pointerId);
    });
    chart?.addEventListener("pointermove", (event) => {
      if (!state.drag) return;
      const rect = chart.getBoundingClientRect();
      const delta = (event.clientX - state.drag.x) / Math.max(1, rect.width);
      state.pan = clamp(state.drag.pan - delta, 0, 1);
      renderChart();
    });
    ["pointerup", "pointercancel", "pointerleave"].forEach((eventName) => chart?.addEventListener(eventName, () => { state.drag = null; }));
    chart?.addEventListener("wheel", (event) => {
      if (!event.ctrlKey) return;
      event.preventDefault();
      state.zoomIndex = clamp(state.zoomIndex + (event.deltaY < 0 ? 1 : -1), 0, ZOOMS.length - 1);
      renderChart();
    }, { passive: false });
    chart?.addEventListener("keydown", (event) => {
      if (event.key === "ArrowLeft") { state.pan = clamp(state.pan - 0.1, 0, 1); renderChart(); event.preventDefault(); }
      if (event.key === "ArrowRight") { state.pan = clamp(state.pan + 0.1, 0, 1); renderChart(); event.preventDefault(); }
      if (event.key === "Home") { state.pan = 0; renderChart(); event.preventDefault(); }
      if (event.key === "End") { state.pan = 1; renderChart(); event.preventDefault(); }
    });

    $("#refreshButton")?.addEventListener("click", async () => {
      await runAction("refresh", async () => {
        const payload = await postJson("/api/refresh", {});
        showToast(payload.accepted ? "已请求立即刷新" : "本轮采样仍在进行", payload.accepted ? "success" : "info");
      });
    });
    $("#exportButton")?.addEventListener("click", async () => {
      await runAction("export", downloadCsv);
    });
    $("#connectionTestButton")?.addEventListener("click", async () => {
      await runAction("connection", async () => {
        const payload = await postJson("/api/connection-test", {
          controller: $("#controllerInput")?.value.trim() || "",
          secret: $("#secretInput")?.value.trim() || "",
        });
        state.connectionState = "ready";
        state.connectionMessage = payload.message || `已发现 ${payload.nodeCount || 0} 个可用节点`;
        state.status = { ...(state.status || {}), availableNodes: payload.availableNodes || [] };
        render();
        showToast(state.connectionMessage, "success");
      });
    });
    $("#pauseButton")?.addEventListener("click", async () => {
      const paused = !Boolean(state.status?.monitor?.paused);
      await runAction("pause", async () => {
        await postJson("/api/pause", { paused });
        showToast(paused ? "监控已暂停" : "监控已继续", "success");
        await refreshStatusOnce();
      });
    });
    $("#stopButton")?.addEventListener("click", async () => {
      if (!window.confirm("停止后，本机监控服务会退出。确定继续吗？")) return;
      await runAction("stop", async () => {
        await postJson("/api/shutdown", {});
        showToast("监控服务正在关闭", "info");
        window.setTimeout(() => { state.online = false; render(); }, 800);
      });
    });
    $("#routeToggle")?.addEventListener("click", async () => {
      const enabled = !Boolean(state.status?.routing?.enabled);
      const hadUnsavedSettings = state.settingsDirty;
      await runAction("route", async () => {
        await postJson("/api/settings", { autoRoute: enabled });
        $("#autoRouteInput").checked = enabled;
        state.settingsDirty = hadUnsavedSettings;
        showToast(enabled ? "自动路由已开启" : "自动路由已关闭", enabled ? "warning" : "info");
        await refreshStatusOnce();
      });
    });
    $("#retryButton")?.addEventListener("click", () => { void refreshStatusOnce(true); });
    $("#loadConfigButton")?.addEventListener("click", async () => {
      await runAction("config", async () => {
        const payload = await fetchJson("/api/config?refresh=1", {}, 7000);
        state.status = { ...(state.status || {}), ...payload, settings: payload.settings || state.status?.settings, availableNodes: payload.availableNodes || [] };
        state.settingsDirty = false;
        hydrateSettings(payload.settings || {}, true);
        render();
        showToast("节点清单已刷新", "success");
      });
    });

    $("#settingsForm")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      await runAction("save", async () => {
        const payload = await postJson("/api/settings", readSettingsForm());
        state.status = { ...(state.status || {}), ...payload, settings: payload.settings || state.status?.settings, availableNodes: payload.availableNodes || state.status?.availableNodes || [] };
        state.settingsDirty = false;
        hydrateSettings(payload.settings || {}, true);
        state.history = null;
        state.historyKey = "";
        render();
        showToast("设置已保存", "success");
        maybeLoadHistory(true);
      });
    });
    $$("#settingsForm input, #settingsForm select").forEach((input) => input.addEventListener("input", markFormDirty));
    $("#selectAllNodes")?.addEventListener("click", () => { state.draftNodes = availableNodes(); renderNodePicker(); markFormDirty(); });
    $("#clearNodes")?.addEventListener("click", () => { state.draftNodes = []; renderNodePicker(); markFormDirty(); });
    $("#nodeFilter")?.addEventListener("change", (event) => { state.nodeFilter = event.target.value; renderNodeTable(); });
  }

  async function refreshStatusOnce(forceHistory = false) {
    try {
      const payload = await fetchJson("/api/status", {}, 3500);
      state.status = payload;
      state.online = true;
      state.checking = false;
      if (!state.formHydrated) hydrateSettings(payload.settings || {});
      render();
      if (forceHistory) { state.history = null; state.historyKey = ""; maybeLoadHistory(true); }
    } catch (error) {
      state.online = false;
      state.checking = false;
      render();
      showToast(error.message || "无法连接本机服务", "error");
    }
  }

  async function runAction(name, action) {
    if (state.action) return;
    state.action = name;
    renderControls();
    try {
      await action();
    } catch (error) {
      if (name === "connection") {
        state.connectionState = "error";
        state.connectionMessage = error.message || "连接测试失败";
        render();
      }
      showToast(error.message || "操作失败", "error");
    } finally {
      state.action = "";
      renderControls();
    }
  }

  function init() {
    initTheme();
    bindEvents();
    renderFocusOptions();
    render();
    void poll();
  }

  document.addEventListener("DOMContentLoaded", init, { once: true });
})();
