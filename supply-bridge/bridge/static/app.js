"use strict";

const state = { token: sessionStorage.getItem("bridgeToken") || "", status: null, settings: null, groups: [] };
const titles = { overview: "运行总览", automation: "自动补货", orders: "订单记录", deliveries: "账号推送", recoveries: "修复与退款", notifications: "飞书通知", audit: "操作审计", access: "连接状态" };
let toastTimer = 0;

const $ = (id) => document.getElementById(id);
const qsa = (selector) => Array.from(document.querySelectorAll(selector));

async function api(path, options = {}) {
  const headers = { "X-Bridge-Token": state.token, ...(options.headers || {}) };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers, cache: "no-store" });
  let data = {};
  try { data = await response.json(); } catch (_) { data = {}; }
  if (response.status === 401) {
    sessionStorage.removeItem("bridgeToken");
    state.token = "";
    showLogin();
    throw new Error("管理口令无效");
  }
  if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
  return data;
}

function showLogin() {
  $("loginView").classList.remove("hidden");
  $("appView").classList.add("hidden");
}

function showApp() {
  $("loginView").classList.add("hidden");
  $("appView").classList.remove("hidden");
}

function toast(message, error = false) {
  const node = $("toast");
  node.textContent = message;
  node.classList.toggle("error", error);
  node.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove("show"), 2800);
}

function setBusy(button, busy) {
  if (!button) return;
  button.disabled = busy;
  button.dataset.label ||= button.textContent;
  button.textContent = busy ? "处理中" : button.dataset.label;
}

function setTab(name) {
  qsa(".nav-item").forEach((node) => node.classList.toggle("active", node.dataset.tab === name));
  qsa(".tab-page").forEach((node) => node.classList.toggle("active", node.id === `tab-${name}`));
  $("pageTitle").textContent = titles[name] || "Supply Bridge";
  if (name === "orders") loadOrders();
  if (name === "deliveries") loadDeliveries();
  if (name === "recoveries") loadRecoveries();
  if (name === "audit") loadEvents();
}

function money(fen) { return `¥${(Number(fen || 0) / 100).toFixed(2)}`; }
function quota(value) { return `$${Number(value || 0).toFixed(2)}`; }
function percent(value) { return `${Math.round(Number(value || 0) * 100)}%`; }
function when(value) {
  if (!value) return "--";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
}
function eta(value) {
  if (value === null || value === undefined) return "--";
  if (value > 1440) return `${(value / 1440).toFixed(1)} 天`;
  if (value > 60) return `${(value / 60).toFixed(1)} 小时`;
  return `${Math.max(0, Math.round(value))} 分钟`;
}

function stateNode(text, kind) {
  const span = document.createElement("span");
  span.className = `state-text ${kind || ""}`;
  span.textContent = text || "--";
  return span;
}

function setStateText(id, text, good, neutral = false) {
  const node = $(id);
  node.textContent = text;
  node.className = `state-text ${neutral ? "neutral" : good ? "good" : "bad"}`;
}

async function refreshStatus(quiet = false) {
  const button = $("refreshButton");
  if (!quiet) setBusy(button, true);
  try {
    const [status, events] = await Promise.all([api("/api/status"), api("/api/events")]);
    state.status = status;
    state.settings = status.settings;
    renderStatus(status);
    renderEvents(events.slice(0, 8), $("recentEventsBody"));
    if (!quiet) toast("状态已刷新");
  } catch (error) {
    if (!quiet) toast(error.message, true);
  } finally {
    if (!quiet) setBusy(button, false);
  }
}

function renderStatus(status) {
  const m = status.metrics || {};
  const s = status.settings || {};
  $("metricAvailable").textContent = m.available_accounts ?? "--";
  $("metricAccountsDetail").textContent = `总数 ${m.total_accounts ?? "--"} · 异常 ${m.error_accounts ?? "--"}`;
  $("metricQuota").textContent = quota(m.effective_quota_usd);
  $("metricRate").textContent = `每分钟 ${quota(m.planning_rate_usd_per_minute)}`;
  $("metricConcurrency").textContent = percent(m.concurrency_utilization);
  $("metricQueue").textContent = `${m.concurrency_used || 0}/${m.concurrency_max || 0} · 排队 ${m.waiting_in_queue || 0}`;
  $("metricEta").textContent = eta(m.eta_minutes);
  $("metricEtaDetail").textContent = m.eta_minutes == null ? "等待形成预测" : `预警 ${s.forecast_lead_minutes || 0} 分钟`;
  $("metricBalance").textContent = money(m.supplier_available_fen);
  $("metricHeld").textContent = `冻结 ${money(m.supplier_held_fen)}`;
  $("metricSpend").textContent = money(status.daily_spend_fen);
  $("metricSpendCap").textContent = `上限 ${money(s.daily_spend_cap_fen)}`;
  setStateText("sub2State", m.sub2_connected ? "正常" : "异常", m.sub2_connected);
  setStateText("externalSupplierState", m.external_supplier_connected ? "正常" : "等待流量", m.external_supplier_connected);
  setStateText("supplierState", !status.supplier_configured ? "未启用" : m.supplier_connected ? "正常" : "异常", m.supplier_connected, !status.supplier_configured);
  $("pollState").textContent = status.last_tick_at ? when(status.last_tick_at) : "等待首次轮询";
  $("activeOrderState").textContent = `${(status.active_orders || []).length} 个`;
  $("lastUpdated").textContent = status.last_tick_at ? `最近同步 ${when(status.last_tick_at)}` : "等待首次同步";
  const stopped = Boolean(s.emergency_stop);
  $("stopBanner").classList.toggle("hidden", !stopped);
  const badge = $("automationBadge");
  badge.textContent = stopped ? "紧急停止" : !s.auto_enabled ? "已暂停" : s.dry_run ? "自动 · 演练" : "自动 · 真实";
  badge.className = `badge ${stopped ? "bad" : s.auto_enabled ? "good" : ""}`;
  $("engineDot").className = `status-dot ${m.sub2_connected ? "online" : "offline"}`;
  $("engineLabel").textContent = m.sub2_connected ? "服务运行中" : "连接异常";
  $("accessSub2Dot").className = `status-dot ${m.sub2_connected ? "online" : "offline"}`;
  $("accessSub2Text").textContent = m.sub2_connected ? "最小权限连接正常" : (status.last_error || "连接异常");
  $("accessExternalSupplierDot").className = `status-dot ${m.external_supplier_connected ? "online" : "offline"}`;
  $("accessExternalSupplierText").textContent = m.external_supplier_connected ? `最近流量 ${when(m.external_supplier_last_seen_at)}` : "等待供应商轮询";
  $("accessSupplierDot").className = `status-dot ${!status.supplier_configured ? "" : m.supplier_connected ? "online" : "offline"}`;
  $("accessSupplierText").textContent = !status.supplier_configured ? "未启用（当前使用外部自动推送）" : m.supplier_connected ? "接口连接正常" : (status.last_error || "连接异常");
}

function cell(row, text, className = "") {
  const td = document.createElement("td");
  td.textContent = text ?? "--";
  if (className) td.className = className;
  row.appendChild(td);
}

function emptyRow(body, columns) {
  const row = document.createElement("tr");
  const td = document.createElement("td");
  td.colSpan = columns;
  td.className = "table-empty";
  td.textContent = "暂无记录";
  row.appendChild(td);
  body.appendChild(row);
}

function statusKind(value) {
  const good = ["completed", "active", "claimed", "ready"];
  const bad = ["failed", "quarantined", "cancelled", "error"];
  return good.includes(value) ? "good" : bad.includes(value) ? "bad" : "warn";
}

function renderEvents(items, body) {
  body.replaceChildren();
  if (!items.length) return emptyRow(body, body === $("eventsBody") ? 5 : 4);
  for (const item of items) {
    const row = document.createElement("tr");
    cell(row, when(item.created_at));
    const level = document.createElement("td");
    level.appendChild(stateNode(item.level, item.level === "error" ? "bad" : item.level === "warning" ? "warn" : "good"));
    row.appendChild(level);
    cell(row, item.event_type);
    cell(row, item.message);
    if (body === $("eventsBody")) cell(row, Object.keys(item.metadata || {}).length ? JSON.stringify(item.metadata) : "--");
    body.appendChild(row);
  }
}

async function loadEvents() {
  try { renderEvents(await api("/api/events"), $("eventsBody")); } catch (error) { toast(error.message, true); }
}

async function loadOrders() {
  try {
    const body = $("ordersBody");
    const items = await api("/api/orders");
    body.replaceChildren();
    if (!items.length) return emptyRow(body, 9);
    for (const item of items) {
      const row = document.createElement("tr");
      cell(row, when(item.created_at)); cell(row, item.product); cell(row, item.quantity); cell(row, item.trigger_type);
      const status = document.createElement("td"); status.appendChild(stateNode(item.status, statusKind(item.status))); row.appendChild(status);
      cell(row, money(item.estimated_fen)); cell(row, money(item.charged_fen)); cell(row, item.last_error || "--", "cell-error");
      const action = document.createElement("td");
      if (!["completed", "partial", "cancelled", "failed", "dry_run"].includes(item.status) && item.supplier_order_id) {
        const button = document.createElement("button"); button.className = "link-button"; button.textContent = "提货 / 重试";
        button.addEventListener("click", () => takeOrder(item.id, button)); action.appendChild(button);
      } else action.textContent = "--";
      row.appendChild(action);
      body.appendChild(row);
    }
  } catch (error) { toast(error.message, true); }
}

async function loadDeliveries() {
  try {
    const body = $("deliveriesBody");
    const items = await api("/api/deliveries");
    body.replaceChildren();
    if (!items.length) return emptyRow(body, 9);
    for (const item of items) {
      const row = document.createElement("tr");
      cell(row, when(item.created_at)); cell(row, item.account_name); cell(row, item.sub2_account_id || "--"); cell(row, quota(item.quota_usd)); cell(row, when(item.expires_at));
      const status = document.createElement("td"); status.appendChild(stateNode(item.status, statusKind(item.status))); row.appendChild(status);
      cell(row, item.attempts); cell(row, item.last_error || "--", "cell-error");
      const action = document.createElement("td");
      if (item.sub2_account_id && item.status !== "active") {
        const button = document.createElement("button"); button.className = "link-button"; button.textContent = "重新验货";
        button.addEventListener("click", () => retryDelivery(item.id, button)); action.appendChild(button);
      } else action.textContent = "--";
      row.appendChild(action); body.appendChild(row);
    }
  } catch (error) { toast(error.message, true); }
}

async function loadRecoveries() {
  try {
    const body = $("recoveriesBody");
    const items = await api("/api/recoveries");
    body.replaceChildren();
    if (!items.length) return emptyRow(body, 5);
    for (const item of items) {
      const row = document.createElement("tr"); cell(row, when(item.updated_at)); cell(row, item.account_name);
      const status = document.createElement("td"); status.appendChild(stateNode(item.status, statusKind(item.status))); row.appendChild(status);
      cell(row, item.attempts); cell(row, item.last_error || "--", "cell-error"); body.appendChild(row);
    }
  } catch (error) { toast(error.message, true); }
}

async function loadSettings() {
  try {
    const [settings, groups] = await Promise.all([api("/api/settings"), api("/api/groups")]);
    state.settings = settings; state.groups = groups.filter((g) => g.platform === "openai");
    fillGroupSelects(settings); fillSettings(settings); fillNotificationSettings(settings);
  } catch (error) { toast(error.message, true); }
}

function fillGroupSelects(settings) {
  for (const id of ["stagingGroup", "monitorGroup", "targetGroups"]) {
    const select = $(id); select.replaceChildren();
    for (const group of state.groups) {
      const option = document.createElement("option"); option.value = group.id; option.textContent = group.name; select.appendChild(option);
    }
  }
  $("stagingGroup").value = String(settings.staging_group_id || "");
  $("monitorGroup").value = String(settings.monitor_group_id || "");
  const targets = new Set((settings.target_group_ids || []).map(String));
  Array.from($("targetGroups").options).forEach((option) => { option.selected = targets.has(option.value); });
}

function fillSettings(s) {
  const checks = { autoEnabled: "auto_enabled", dryRun: "dry_run", openaiPassthrough: "openai_passthrough", triggerLow: "replenish_on_low_stock", triggerEta: "replenish_on_eta", triggerConcurrency: "replenish_on_concurrency", triggerEmpty: "replenish_on_empty", triggerSchedule: "replenish_on_schedule" };
  for (const [id, key] of Object.entries(checks)) $(id).checked = Boolean(s[key]);
  const values = { lowWatermark: "low_watermark", targetAvailable: "target_available", minOrderUnits: "min_order_units", maxOrderUnits: "max_order_units", cooldownSeconds: "cooldown_seconds", forecastLead: "forecast_lead_minutes", concurrencyThreshold: "concurrency_threshold_percent", scheduleInterval: "schedule_interval_minutes", scheduleQuantity: "schedule_quantity", pollInterval: "poll_interval_seconds", accountConcurrency: "account_concurrency" };
  for (const [id, key] of Object.entries(values)) $(id).value = s[key];
  $("dailySpendCap").value = (Number(s.daily_spend_cap_fen || 0) / 100).toFixed(2);
  $("product1h").checked = (s.products || []).includes("team_1h");
  $("product30").checked = (s.products || []).includes("oauth_30d");
  $("product7").checked = (s.products || []).includes("oauth_7d");
  $("models").value = (s.models || []).join("\n");
}

function fillNotificationSettings(s) {
  $("feishuEnabled").checked = Boolean(s.feishu_enabled || s.webhook_enabled);
  $("feishuNotifyPool").checked = Boolean(s.feishu_notify_pool);
  $("feishuNotifyBalance").checked = Boolean(s.feishu_notify_balance);
  $("feishuNotifyOrders").checked = Boolean(s.feishu_notify_orders);
  $("feishuNotifyRecoveries").checked = Boolean(s.feishu_notify_recoveries);
  $("feishuBalanceThreshold").value = (Number(s.feishu_balance_threshold_fen || 0) / 100).toFixed(2);
  $("feishuCooldownMinutes").value = Math.max(1, Math.round(Number(s.feishu_cooldown_seconds || 600) / 60));
  $("feishuWebhook").value = "";
  $("feishuSecret").value = "";
  $("feishuWebhook").placeholder = s.feishu_webhook_configured ? "已配置，留空保持不变" : "https://open.feishu.cn/open-apis/bot/v2/hook/...";
  $("feishuSecret").placeholder = s.feishu_signing_secret_configured ? "已配置，留空保持不变" : "可选";
  const configured = Boolean(s.feishu_webhook_configured);
  const enabled = configured && $("feishuEnabled").checked;
  $("feishuStateDot").className = `status-dot ${enabled ? "online" : "offline"}`;
  $("feishuStateTitle").textContent = enabled ? "通知运行中" : configured ? "通知已暂停" : "待配置";
  $("feishuStateText").textContent = configured ? (s.feishu_signing_secret_configured ? "Webhook 与签名已配置" : "Webhook 已配置") : "Webhook 未配置";
}

function collectSettings() {
  const int = (id) => Number.parseInt($(id).value, 10);
  return {
    auto_enabled: $("autoEnabled").checked, dry_run: $("dryRun").checked, openai_passthrough: $("openaiPassthrough").checked,
    replenish_on_low_stock: $("triggerLow").checked, replenish_on_eta: $("triggerEta").checked, replenish_on_concurrency: $("triggerConcurrency").checked, replenish_on_empty: $("triggerEmpty").checked, replenish_on_schedule: $("triggerSchedule").checked,
    low_watermark: int("lowWatermark"), target_available: int("targetAvailable"), min_order_units: int("minOrderUnits"), max_order_units: int("maxOrderUnits"),
    daily_spend_cap_fen: Math.round(Number($("dailySpendCap").value) * 100), cooldown_seconds: int("cooldownSeconds"), forecast_lead_minutes: int("forecastLead"), concurrency_threshold_percent: int("concurrencyThreshold"), schedule_interval_minutes: int("scheduleInterval"), schedule_quantity: int("scheduleQuantity"), poll_interval_seconds: int("pollInterval"), account_concurrency: int("accountConcurrency"),
    products: [$("product1h").checked ? "team_1h" : "", $("product30").checked ? "oauth_30d" : "", $("product7").checked ? "oauth_7d" : ""].filter(Boolean),
    staging_group_id: int("stagingGroup"), monitor_group_id: int("monitorGroup"), target_group_ids: Array.from($("targetGroups").selectedOptions).map((o) => Number.parseInt(o.value, 10)),
    models: $("models").value.split(/\r?\n/).map((v) => v.trim()).filter(Boolean),
  };
}

async function saveSettings(event) {
  event.preventDefault();
  const button = event.submitter;
  setBusy(button, true);
  try {
    const settings = await api("/api/settings", { method: "PUT", body: JSON.stringify(collectSettings()) });
    state.settings = settings; fillSettings(settings); toast("配置已保存"); await refreshStatus(true);
  } catch (error) { toast(error.message, true); } finally { setBusy(button, false); }
}

function collectNotificationSettings() {
  const body = {
    feishu_enabled: $("feishuEnabled").checked,
    feishu_balance_threshold_fen: Math.round(Number($("feishuBalanceThreshold").value) * 100),
    feishu_cooldown_seconds: Math.round(Number($("feishuCooldownMinutes").value) * 60),
    feishu_notify_pool: $("feishuNotifyPool").checked,
    feishu_notify_balance: $("feishuNotifyBalance").checked,
    feishu_notify_orders: $("feishuNotifyOrders").checked,
    feishu_notify_recoveries: $("feishuNotifyRecoveries").checked,
  };
  const webhook = $("feishuWebhook").value.trim();
  const secret = $("feishuSecret").value.trim();
  if (webhook) body.feishu_webhook_url = webhook;
  if (secret) body.feishu_signing_secret = secret;
  return body;
}

async function updateNotificationSettings() {
  const settings = await api("/api/settings", { method: "PUT", body: JSON.stringify(collectNotificationSettings()) });
  state.settings = settings;
  fillNotificationSettings(settings);
  return settings;
}

async function saveNotificationSettings(event) {
  event.preventDefault();
  const button = event.submitter; setBusy(button, true);
  try { await updateNotificationSettings(); toast("飞书通知配置已保存"); }
  catch (error) { toast(error.message, true); } finally { setBusy(button, false); }
}

async function testFeishu() {
  const button = $("testFeishu"); setBusy(button, true);
  try {
    await updateNotificationSettings();
    const result = await api("/api/notifications/test", { method: "POST", body: "{}" });
    toast(result.message || "测试通知已发送");
  } catch (error) { toast(error.message, true); } finally { setBusy(button, false); }
}

async function clearFeishu() {
  if (!window.confirm("确认清除飞书 Webhook 和签名密钥？")) return;
  const button = $("clearFeishu"); setBusy(button, true);
  try {
    const settings = await api("/api/notifications/clear", { method: "POST", body: "{}" });
    state.settings = settings; fillNotificationSettings(settings); toast("飞书通知凭据已清除");
  } catch (error) { toast(error.message, true); } finally { setBusy(button, false); }
}

async function runPoll() {
  const button = $("pollButton"); setBusy(button, true);
  try { await api("/api/actions/run", { method: "POST", body: "{}" }); toast("检测完成"); await refreshStatus(true); }
  catch (error) { toast(error.message, true); } finally { setBusy(button, false); }
}

async function takeOrder(orderId, button) {
  setBusy(button, true);
  try { await api("/api/actions/take-order", { method: "POST", body: JSON.stringify({ order_id: orderId }) }); toast("订单状态已更新"); await Promise.all([loadOrders(), loadDeliveries(), refreshStatus(true)]); }
  catch (error) { toast(error.message, true); } finally { setBusy(button, false); }
}

async function retryDelivery(deliveryId, button) {
  setBusy(button, true);
  try { await api("/api/actions/retry-delivery", { method: "POST", body: JSON.stringify({ delivery_id: deliveryId }) }); toast("账号验货完成"); await loadDeliveries(); }
  catch (error) { toast(error.message, true); } finally { setBusy(button, false); }
}

async function emergencyStop(enabled) {
  if (enabled && !window.confirm("确认立即停止自动下单？状态查询和审计仍会运行。")) return;
  const button = enabled ? $("emergencyButton") : $("resumeBannerButton"); setBusy(button, true);
  try { await api(enabled ? "/api/actions/emergency-stop" : "/api/actions/resume", { method: "POST", body: "{}" }); toast(enabled ? "紧急停止已开启" : "自动运行已恢复"); await refreshStatus(true); }
  catch (error) { toast(error.message, true); } finally { setBusy(button, false); }
}

function openManualDialog() { $("manualOrderDialog").showModal(); }

async function manualOrder(event) {
  event.preventDefault();
  const live = $("manualLive").checked;
  if (live && !window.confirm("这是一个真实订单，将扣除供应商余额。确认继续？")) return;
  const button = event.submitter; setBusy(button, true);
  try {
    await api("/api/actions/manual-order", { method: "POST", body: JSON.stringify({ product: $("manualProduct").value, quantity: Number.parseInt($("manualQuantity").value, 10), dry_run: !live }) });
    $("manualOrderDialog").close(); toast(live ? "真实订单已提交" : "演练订单已生成"); await Promise.all([refreshStatus(true), loadOrders()]);
  } catch (error) { toast(error.message, true); } finally { setBusy(button, false); }
}

function bind() {
  $("loginForm").addEventListener("submit", async (event) => {
    event.preventDefault(); const token = $("accessToken").value.trim(); if (!token) return;
    state.token = token;
    try { await api("/api/status"); sessionStorage.setItem("bridgeToken", token); $("loginError").textContent = ""; showApp(); await Promise.all([refreshStatus(true), loadSettings()]); }
    catch (error) { $("loginError").textContent = error.message; }
  });
  qsa(".nav-item").forEach((node) => node.addEventListener("click", () => setTab(node.dataset.tab)));
  qsa("[data-tab-jump]").forEach((node) => node.addEventListener("click", () => setTab(node.dataset.tabJump)));
  $("refreshButton").addEventListener("click", () => refreshStatus());
  $("pollButton").addEventListener("click", runPoll);
  $("settingsForm").addEventListener("submit", saveSettings);
  $("notificationForm").addEventListener("submit", saveNotificationSettings);
  $("testFeishu").addEventListener("click", testFeishu);
  $("clearFeishu").addEventListener("click", clearFeishu);
  $("emergencyButton").addEventListener("click", () => emergencyStop(true));
  $("resumeBannerButton").addEventListener("click", () => emergencyStop(false));
  $("openManualOrder").addEventListener("click", openManualDialog);
  qsa("[data-open-manual]").forEach((node) => node.addEventListener("click", openManualDialog));
  $("closeManualOrder").addEventListener("click", () => $("manualOrderDialog").close());
  $("cancelManualOrder").addEventListener("click", () => $("manualOrderDialog").close());
  $("manualOrderForm").addEventListener("submit", manualOrder);
  $("logoutButton").addEventListener("click", () => { sessionStorage.removeItem("bridgeToken"); state.token = ""; showLogin(); });
}

async function boot() {
  bind();
  if (!state.token) return showLogin();
  try { showApp(); await Promise.all([refreshStatus(true), loadSettings()]); setInterval(() => refreshStatus(true), 15000); }
  catch (_) { showLogin(); }
}

boot();
