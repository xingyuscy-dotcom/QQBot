let activeConversation = null;
let commandListExpanded = true;

function text(value, fallback = "未获取") {
  return value === null || value === undefined || value === "" ? fallback : String(value);
}

function escapeHtml(value) {
  return text(value, "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function scopeLabel(scopeType) {
  return scopeType === "group" ? "群聊" : "私聊";
}

function enabledLabel(value) {
  return Number(value) === 1 ? "已启用" : "未启用";
}

function learningLabel(value) {
  return Number(value) === 1 ? "学习开" : "学习关";
}

function learningBatchLabel(value) {
  const batchSize = Number(value || 0);
  return batchSize > 0 ? `批量 ${batchSize}` : "批量 全局";
}

function responseModeLabel(item) {
  if (item.scope_type === "private") {
    return "全部消息";
  }
  if (item.response_mode === "all") {
    return "全部消息";
  }
  if (item.response_mode === "prefix") {
    return `前缀：${text(item.trigger_prefix, "/bot")}`;
  }
  return "@机器人";
}

function probabilityPercent(value) {
  return Math.round((Number(value) || 0) * 100);
}

function conversationApiPath(item, suffix = "") {
  const parts = [item.botQq, item.scopeType, item.scopeId].map(encodeURIComponent);
  return `/api/conversations/${parts.join("/")}${suffix}`;
}

function levelLabel(level) {
  const labels = {
    info: "信息",
    warning: "警告",
    error: "错误",
  };
  return labels[level] || text(level, "-");
}

function formatLogDetail(detail) {
  const raw = text(detail, "");
  if (!raw) return "-";
  try {
    const data = JSON.parse(raw);
    return Object.entries(data)
      .map(([key, value]) => `${key}: ${value}`)
      .join("；");
  } catch {
    return raw;
  }
}

function commandScopeLabel(scopes) {
  const labels = {
    group: "群聊",
    private: "私聊",
  };
  return (scopes || []).map((scope) => labels[scope] || scope).join("、") || "-";
}

function readCommandEditorItems() {
  const content = document.querySelector("#commandEditor").value.trim();
  if (!content) return [];
  const items = JSON.parse(content);
  if (!Array.isArray(items)) {
    throw new Error("命令库必须是 JSON 数组");
  }
  return items;
}

function writeCommandEditorItems(items) {
  document.querySelector("#commandEditor").value = JSON.stringify(items, null, 2);
  renderCommandList(items);
}

function makeCommandTemplate(items) {
  const index = items.length + 1;
  return {
    name: `new_command_${index}`,
    trigger: `/新命令${index}`,
    usage: `/新命令${index}`,
    description: "新命令说明",
    manager_only: true,
    scopes: ["group", "private"],
    handler: "command_help",
  };
}

function formatBytes(size) {
  const value = Number(size) || 0;
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function formatNumber(value) {
  return String(Number(value || 0).toLocaleString("zh-CN"));
}

function okText(value, okLabel = "正常", badLabel = "异常") {
  return value ? okLabel : badLabel;
}

function directoryHealthText(directories) {
  const items = Object.entries(directories || {});
  if (!items.length) return "未获取";
  const bad = items.filter(([, item]) => !item?.ok).map(([name]) => name);
  return bad.length ? `异常：${bad.join("、")}` : "正常";
}

function modelTestText(modelTest) {
  if (!modelTest) return "未测试";
  return `${okText(modelTest.ok)} ${text(modelTest.tested_at, "")}`.trim();
}

async function refreshStatus() {
  const statusGrid = document.querySelector("#statusGrid");
  const healthNotice = document.querySelector("#healthNotice");
  const [statusResponse, healthResponse] = await Promise.all([
    fetch("/api/status"),
    fetch("/api/health"),
  ]);
  const data = await statusResponse.json();
  const health = await healthResponse.json();
  const napcat = health.napcat || {};
  const onebot = health.onebot || {};
  const llmConfig = health.llm_config || {};
  const database = health.database || {};
  const latestBot = napcat.latest_bot || data.latest_bot || {};

  statusGrid.innerHTML = [
    ["服务", data.service],
    ["数据库", okText(database.ok && data.database_exists)],
    ["NapCat", okText(napcat.connected, "已连接", "未连接")],
    ["机器人 QQ", latestBot.bot_qq || "未获取"],
    ["OneBot", `${okText(onebot.listening)} ${onebot.address || `${data.onebot_port} ${data.onebot_path}`}`],
    ["模型配置", okText(llmConfig.ok, "完整", "不完整")],
    ["模型测试", modelTestText(health.model_test)],
    ["目录写入", directoryHealthText(health.directories)],
    ["会话数", String(data.conversation_count)],
    ["群聊 / 私聊", `${data.group_count} / ${data.private_count}`],
    ["消息数", String(data.message_count)],
    ["后台端口", data.admin_port],
  ]
    .map(([label, value]) => `<div><dt>${label}</dt><dd>${escapeHtml(value)}</dd></div>`)
    .join("");

  const notices = [];
  if (!llmConfig.ok && llmConfig.missing?.length) {
    notices.push(`模型配置缺少：${llmConfig.missing.join("、")}`);
  }
  if (health.model_test && !health.model_test.ok) {
    notices.push(`最近模型测试失败：${health.model_test.detail}`);
  }
  if (health.model_test && health.model_test.ok) {
    notices.push(
      `最近模型测试成功：${text(health.model_test.detail, "OK")} ${text(health.model_test.tested_at, "")}`.trim()
    );
  }
  healthNotice.textContent = notices.join("；");
}

async function testModelConnection() {
  const button = document.querySelector("#testModel");
  const notice = document.querySelector("#healthNotice");

  button.disabled = true;
  notice.textContent = "模型连接测试中";
  try {
    const response = await fetch("/api/health/model-test", { method: "POST" });
    if (!response.ok) throw new Error(await readErrorMessage(response, "模型测试失败"));

    const data = await response.json();
    const result = data.result || {};
    notice.textContent = result.ok
      ? `模型连接正常：${text(result.detail, "OK")}`
      : `模型连接失败：${text(result.detail, "详情看日志")}`;
    await refreshStatus();
    await refreshLogs();
  } catch (error) {
    notice.textContent = `模型测试失败：${error.message || "详情看后台日志"}`;
  } finally {
    button.disabled = false;
  }
}

async function refreshBackups() {
  const list = document.querySelector("#backupList");
  const response = await fetch("/api/backups");
  const data = await response.json();

  document.querySelector("#backupDir").textContent = data.backups_dir
    ? `保存位置：${data.backups_dir}`
    : "";

  if (!data.items.length) {
    list.innerHTML = `<tr><td colspan="5" class="empty">暂无备份</td></tr>`;
    return;
  }

  list.innerHTML = data.items
    .map(
      (item) => `
        <tr>
          <td>${escapeHtml(item.name)}</td>
          <td>${escapeHtml(formatBytes(item.size))}</td>
          <td>${escapeHtml(text(item.created_at, "-"))}</td>
          <td>${escapeHtml(item.path)}</td>
          <td>
            <div class="action-buttons">
              <button
                class="backup-inspect-button secondary"
                type="button"
                data-name="${escapeHtml(item.name)}"
              >
                查看
              </button>
              <button
                class="backup-restore-button danger"
                type="button"
                data-name="${escapeHtml(item.name)}"
              >
                恢复
              </button>
            </div>
          </td>
        </tr>
      `
    )
    .join("");
}

async function createBackup() {
  const button = document.querySelector("#createBackup");
  const notice = document.querySelector("#backupNotice");

  button.disabled = true;
  notice.textContent = "备份中";
  try {
    const response = await fetch("/api/backups", { method: "POST" });
    if (!response.ok) {
      let message = "备份失败";
      try {
        const errorData = await response.json();
        message = errorData.detail || message;
      } catch {
        message = await response.text();
      }
      throw new Error(message);
    }

    const data = await response.json();
    notice.textContent = `备份完成：${data.backup?.name || ""}`;
    await refreshBackups();
    await refreshLogs();
  } catch (error) {
    notice.textContent = `备份失败：${error.message || "详情看后台日志"}`;
  } finally {
    button.disabled = false;
  }
}

async function inspectBackup(button) {
  const notice = document.querySelector("#backupNotice");
  const name = button.dataset.name;

  button.disabled = true;
  notice.textContent = "读取备份中";
  try {
    const response = await fetch(`/api/backups/${encodeURIComponent(name)}`);
    if (!response.ok) throw new Error(await readErrorMessage(response, "读取备份失败"));

    const data = await response.json();
    const contains = data.contains || {};
    notice.textContent =
      `备份内容：数据库${contains.database ? "有" : "无"}，命令库${contains.commands ? "有" : "无"}，` +
      `记忆文件 ${contains.memory_files || 0} 个，日志文件 ${contains.log_files || 0} 个。`;
  } catch (error) {
    notice.textContent = `读取备份失败：${error.message || "详情看后台日志"}`;
  } finally {
    button.disabled = false;
  }
}

async function restoreBackup(button) {
  const notice = document.querySelector("#backupNotice");
  const name = button.dataset.name;
  const confirmText = window.prompt(`将用这个备份覆盖当前数据：\n${name}\n\n确认恢复请输入 RESTORE`);
  if (confirmText === null) return;
  if (confirmText.trim() !== "RESTORE") {
    notice.textContent = "恢复已取消：确认文本不正确";
    return;
  }

  button.disabled = true;
  notice.textContent = "恢复中，恢复前会自动保存当前状态";
  try {
    const response = await fetch(`/api/backups/${encodeURIComponent(name)}/restore`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm_text: confirmText }),
    });
    if (!response.ok) throw new Error(await readErrorMessage(response, "恢复失败"));

    const data = await response.json();
    notice.textContent = `已恢复：${data.backup?.name || name}。恢复前安全备份：${data.safety_backup?.name || ""}`;
    await refreshAll();
  } catch (error) {
    notice.textContent = `恢复失败：${error.message || "详情看后台日志"}`;
  } finally {
    button.disabled = false;
  }
}

async function readErrorMessage(response, fallback) {
  try {
    const errorData = await response.json();
    return errorData.detail || fallback;
  } catch {
    return await response.text();
  }
}

async function refreshLogs() {
  const list = document.querySelector("#runtimeLogList");
  const response = await fetch("/api/logs?limit=100");
  const data = await response.json();

  if (!data.items.length) {
    list.innerHTML = `<tr><td colspan="5" class="empty">暂无日志</td></tr>`;
    return;
  }

  list.innerHTML = data.items
    .map(
      (item) => `
        <tr class="log-row log-${escapeHtml(item.level)}">
          <td>${escapeHtml(text(item.created_at, "-"))}</td>
          <td><span class="log-level">${escapeHtml(levelLabel(item.level))}</span></td>
          <td>${escapeHtml(text(item.scope, "-"))}</td>
          <td>${escapeHtml(text(item.message, "-"))}</td>
          <td class="log-detail">${escapeHtml(formatLogDetail(item.detail))}</td>
        </tr>
      `
    )
    .join("");
}

async function refreshCommands(clearNotice = false) {
  const list = document.querySelector("#commandList");
  const editor = document.querySelector("#commandEditor");
  const notice = document.querySelector("#commandNotice");

  try {
    const response = await fetch("/api/commands");
    if (!response.ok) throw new Error(await readErrorMessage(response, "读取命令库失败"));

    const data = await response.json();
    document.querySelector("#commandPath").textContent = data.path
      ? `本地文件：${data.path}`
      : "";
    editor.value = data.content || "";

    if (!data.items.length) {
      list.innerHTML = `<tr><td colspan="7" class="empty">命令库为空</td></tr>`;
      if (clearNotice) notice.textContent = "";
      return;
    }

    renderCommandList(data.items);
    if (clearNotice) notice.textContent = "";
  } catch (error) {
    list.innerHTML = `<tr><td colspan="7" class="empty">读取失败</td></tr>`;
    notice.textContent = `读取命令库失败：${error.message || "详情看后台日志"}`;
  }
}

function renderCommandList(items) {
  const list = document.querySelector("#commandList");
  const tableWrap = document.querySelector("#commandTableWrap");
  const toggleButton = document.querySelector("#toggleCommandList");

  tableWrap.hidden = !commandListExpanded;
  toggleButton.textContent = commandListExpanded ? "收起命令列表" : "展开命令列表";

  if (!items.length) {
    list.innerHTML = `<tr><td colspan="7" class="empty">命令库为空</td></tr>`;
    return;
  }

  list.innerHTML = items
    .map(
      (item, index) => `
        <tr>
          <td>${escapeHtml(item.trigger)}</td>
          <td>${escapeHtml(item.usage)}</td>
          <td>${escapeHtml(text(item.description, "-"))}</td>
          <td>${item.manager_only ? "管理员" : "所有人"}</td>
          <td>${escapeHtml(commandScopeLabel(item.scopes))}</td>
          <td>${escapeHtml(item.handler)}</td>
          <td>
            <button
              class="delete-command-button danger"
              type="button"
              data-index="${index}"
            >
              删除
            </button>
          </td>
        </tr>
      `
    )
    .join("");
}

async function saveCommands() {
  const button = document.querySelector("#saveCommands");
  const notice = document.querySelector("#commandNotice");
  const content = document.querySelector("#commandEditor").value;

  button.disabled = true;
  notice.textContent = "保存中";
  try {
    const response = await fetch("/api/commands", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
    if (!response.ok) throw new Error(await readErrorMessage(response, "保存命令库失败"));

    const data = await response.json();
    document.querySelector("#commandEditor").value = data.content || content;
    notice.textContent = `已保存，共 ${data.items.length} 条命令`;
    await refreshCommands();
    await refreshLogs();
  } catch (error) {
    notice.textContent = `保存命令库失败：${error.message || "详情看后台日志"}`;
  } finally {
    button.disabled = false;
  }
}

function toggleCommandList() {
  commandListExpanded = !commandListExpanded;
  try {
    renderCommandList(readCommandEditorItems());
  } catch {
    document.querySelector("#commandTableWrap").hidden = !commandListExpanded;
    document.querySelector("#toggleCommandList").textContent = commandListExpanded
      ? "收起命令列表"
      : "展开命令列表";
  }
}

function addCommand() {
  const notice = document.querySelector("#commandNotice");
  try {
    const items = readCommandEditorItems();
    items.push(makeCommandTemplate(items));
    writeCommandEditorItems(items);
    notice.textContent = "已添加到编辑区，保存命令库后生效";
  } catch (error) {
    notice.textContent = `添加失败：${error.message || "请先修复 JSON"}`;
  }
}

function deleteCommand(button) {
  const notice = document.querySelector("#commandNotice");
  const index = Number(button.dataset.index);
  try {
    const items = readCommandEditorItems();
    if (index < 0 || index >= items.length) {
      throw new Error("命令不存在");
    }
    const removed = items.splice(index, 1)[0];
    writeCommandEditorItems(items);
    notice.textContent = `已删除 ${removed.trigger || removed.name || "命令"}，保存命令库后生效`;
  } catch (error) {
    notice.textContent = `删除失败：${error.message || "请先修复 JSON"}`;
  }
}

async function refreshSettings() {
  const form = document.querySelector("#settingsForm");
  const response = await fetch("/api/settings");
  const data = await response.json();

  form.elements.llm_base_url.value = text(data.llm_base_url, "https://api.deepseek.com");
  form.elements.llm_model.value = text(data.llm_model, "deepseek-v4-flash");
  form.elements.llm_temperature.value = text(data.llm_temperature, "0.8");
  form.elements.llm_max_tokens.value = text(data.llm_max_tokens, "800");
  form.elements.bot_manager_qqs.value = text(data.bot_manager_qqs, "");
  form.elements.bot_memory_batch_size.value = text(data.bot_memory_batch_size, "40");
  form.elements.llm_api_key.placeholder = data.api_key_saved
    ? "API Key 已保存，留空不修改"
    : "请输入 API Key";
  form.elements.bot_global_system_prompt.value = text(
    data.bot_global_system_prompt,
    "你是一个会根据会话整体风格自然回复的 QQ 机器人。"
  );
}

async function refreshConversations() {
  const list = document.querySelector("#conversationList");
  const response = await fetch("/api/conversations");
  const data = await response.json();

  if (!data.items.length) {
    list.innerHTML = `<tr><td colspan="10" class="empty">还没有收到群聊或私聊消息</td></tr>`;
    return;
  }

  list.innerHTML = data.items
    .map(
      (item) => `
        <tr>
          <td>${scopeLabel(item.scope_type)}</td>
          <td>${escapeHtml(item.display_name || (item.scope_type === "group" ? `群 ${item.scope_id}` : `私聊 ${item.scope_id}`))}</td>
          <td>${escapeHtml(item.bot_qq)}</td>
          <td>${escapeHtml(item.scope_id)}</td>
          <td>
            <div>${enabledLabel(item.enabled)}</div>
            <div class="subtle">${learningLabel(item.learning_enabled)}</div>
            <div class="subtle">${learningBatchLabel(item.learning_batch_size)}</div>
          </td>
          <td>
            ${
              item.scope_type === "group"
                ? `
                  <select
                    class="mode-select"
                    data-bot-qq="${escapeHtml(item.bot_qq)}"
                    data-scope-type="${escapeHtml(item.scope_type)}"
                    data-scope-id="${escapeHtml(item.scope_id)}"
                    data-trigger-prefix="${escapeHtml(text(item.trigger_prefix, "/bot"))}"
                  >
                    <option value="mention" ${item.response_mode === "mention" ? "selected" : ""}>@机器人</option>
                    <option value="prefix" ${item.response_mode === "prefix" ? "selected" : ""}>前缀</option>
                    <option value="all" ${item.response_mode === "all" ? "selected" : ""}>全部消息</option>
                  </select>
                  <input
                    class="prefix-input"
                    value="${escapeHtml(text(item.trigger_prefix, "/bot"))}"
                    data-bot-qq="${escapeHtml(item.bot_qq)}"
                    data-scope-type="${escapeHtml(item.scope_type)}"
                    data-scope-id="${escapeHtml(item.scope_id)}"
                    ${item.response_mode === "prefix" ? "" : "disabled"}
                  >
                `
                : responseModeLabel(item)
            }
          </td>
          <td>
            <div class="rate-controls">
              <label>
                <span>冷却秒</span>
                <input
                  class="rate-input"
                  type="number"
                  min="0"
                  step="1"
                  value="${escapeHtml(text(item.reply_cooldown_seconds, "0"))}"
                  data-rate-field="reply_cooldown_seconds"
                  data-bot-qq="${escapeHtml(item.bot_qq)}"
                  data-scope-type="${escapeHtml(item.scope_type)}"
                  data-scope-id="${escapeHtml(item.scope_id)}"
                >
              </label>
              <label>
                <span>概率%</span>
                <input
                  class="rate-input"
                  type="number"
                  min="0"
                  max="100"
                  step="1"
                  value="${escapeHtml(String(probabilityPercent(item.reply_probability)))}"
                  data-rate-field="reply_probability_percent"
                  data-bot-qq="${escapeHtml(item.bot_qq)}"
                  data-scope-type="${escapeHtml(item.scope_type)}"
                  data-scope-id="${escapeHtml(item.scope_id)}"
                >
              </label>
              <label>
                <span>每小时</span>
                <input
                  class="rate-input"
                  type="number"
                  min="0"
                  step="1"
                  value="${escapeHtml(text(item.hourly_reply_limit, "0"))}"
                  data-rate-field="hourly_reply_limit"
                  data-bot-qq="${escapeHtml(item.bot_qq)}"
                  data-scope-type="${escapeHtml(item.scope_type)}"
                  data-scope-id="${escapeHtml(item.scope_id)}"
                >
              </label>
              <div class="subtle">本小时 ${Number(item.hourly_reply_count || 0)} 次</div>
            </div>
          </td>
          <td>${item.message_count}</td>
          <td>${escapeHtml(text(item.last_message_at, "-"))}</td>
          <td>
            <div class="action-buttons">
              <button
                class="persona-button secondary"
                type="button"
                data-bot-qq="${escapeHtml(item.bot_qq)}"
                data-scope-type="${escapeHtml(item.scope_type)}"
                data-scope-id="${escapeHtml(item.scope_id)}"
                data-display-name="${escapeHtml(item.display_name || item.scope_id)}"
              >
                人设
              </button>
              <button
                class="learning-button secondary"
                type="button"
                data-bot-qq="${escapeHtml(item.bot_qq)}"
                data-scope-type="${escapeHtml(item.scope_type)}"
                data-scope-id="${escapeHtml(item.scope_id)}"
                data-learning-enabled="${Number(item.learning_enabled) === 1 ? "0" : "1"}"
              >
                ${Number(item.learning_enabled) === 1 ? "停学" : "学习"}
              </button>
              <button
                class="toggle-button ${Number(item.enabled) === 1 ? "danger" : ""}"
                type="button"
                data-bot-qq="${escapeHtml(item.bot_qq)}"
                data-scope-type="${escapeHtml(item.scope_type)}"
                data-scope-id="${escapeHtml(item.scope_id)}"
                data-enabled="${Number(item.enabled) === 1 ? "0" : "1"}"
              >
                ${Number(item.enabled) === 1 ? "关闭" : "启用"}
              </button>
            </div>
          </td>
        </tr>
      `
    )
    .join("");
}

async function refreshStats() {
  const days = document.querySelector("#statsDays").value || "7";
  const list = document.querySelector("#statsList");
  const response = await fetch(`/api/stats/conversations?days=${encodeURIComponent(days)}`);
  const data = await response.json();

  drawStatsChart(data);

  if (!data.items.length) {
    list.innerHTML = `<tr><td colspan="9" class="empty">暂无统计数据</td></tr>`;
    return;
  }

  list.innerHTML = data.items
    .map(
      (item) => `
        <tr>
          <td>${scopeLabel(item.scope_type)}</td>
          <td>${escapeHtml(item.display_name || (item.scope_type === "group" ? `群 ${item.scope_id}` : `私聊 ${item.scope_id}`))}</td>
          <td>${escapeHtml(item.scope_id)}</td>
          <td>${formatNumber(item.input_messages)}</td>
          <td>${formatNumber(item.output_messages)}</td>
          <td>${formatNumber(item.llm_requests)}</td>
          <td>${formatNumber(item.prompt_tokens)}</td>
          <td>${formatNumber(item.completion_tokens)}</td>
          <td>${formatNumber(item.total_tokens)}</td>
        </tr>
      `
    )
    .join("");
}

function drawStatsChart(data) {
  const canvas = document.querySelector("#statsChart");
  const ctx = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  const padding = { left: 54, right: 24, top: 24, bottom: 42 };
  const labels = data.labels || [];
  const series = buildTotalSeries(data.items || [], labels);
  const maxValue = Math.max(1, ...series.input, ...series.output, ...series.requests);

  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, width, height);
  ctx.strokeStyle = "#d7dde5";
  ctx.lineWidth = 1;
  ctx.strokeRect(0.5, 0.5, width - 1, height - 1);

  drawChartGrid(ctx, width, height, padding, maxValue);
  drawLine(ctx, series.input, labels, maxValue, padding, width, height, "#0f766e");
  drawLine(ctx, series.output, labels, maxValue, padding, width, height, "#2563eb");
  drawLine(ctx, series.requests, labels, maxValue, padding, width, height, "#b45309");
  drawChartLabels(ctx, labels, padding, width, height);
  drawChartLegend(ctx);
}

function buildTotalSeries(items, labels) {
  const totals = {
    input: labels.map(() => 0),
    output: labels.map(() => 0),
    requests: labels.map(() => 0),
  };
  const indexByDate = new Map(labels.map((label, index) => [label, index]));
  for (const item of items) {
    for (const point of item.series || []) {
      const index = indexByDate.get(point.date);
      if (index === undefined) continue;
      totals.input[index] += Number(point.input_messages || 0);
      totals.output[index] += Number(point.output_messages || 0);
      totals.requests[index] += Number(point.llm_requests || 0);
    }
  }
  return totals;
}

function drawChartGrid(ctx, width, height, padding, maxValue) {
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  ctx.font = "12px Microsoft YaHei, Segoe UI, sans-serif";
  ctx.fillStyle = "#64748b";
  ctx.strokeStyle = "#e5e7eb";
  for (let i = 0; i <= 4; i += 1) {
    const y = padding.top + (chartHeight * i) / 4;
    const value = Math.round(maxValue - (maxValue * i) / 4);
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(padding.left + chartWidth, y);
    ctx.stroke();
    ctx.fillText(String(value), 10, y + 4);
  }
}

function drawLine(ctx, values, labels, maxValue, padding, width, height, color) {
  if (!labels.length) return;
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  const xStep = labels.length > 1 ? chartWidth / (labels.length - 1) : 0;
  ctx.strokeStyle = color;
  ctx.fillStyle = color;
  ctx.lineWidth = 2;
  ctx.beginPath();
  values.forEach((value, index) => {
    const x = padding.left + xStep * index;
    const y = padding.top + chartHeight - (Number(value || 0) / maxValue) * chartHeight;
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  values.forEach((value, index) => {
    const x = padding.left + xStep * index;
    const y = padding.top + chartHeight - (Number(value || 0) / maxValue) * chartHeight;
    ctx.beginPath();
    ctx.arc(x, y, 3, 0, Math.PI * 2);
    ctx.fill();
  });
}

function drawChartLabels(ctx, labels, padding, width, height) {
  if (!labels.length) return;
  const chartWidth = width - padding.left - padding.right;
  const xStep = labels.length > 1 ? chartWidth / (labels.length - 1) : 0;
  ctx.fillStyle = "#64748b";
  ctx.font = "12px Microsoft YaHei, Segoe UI, sans-serif";
  labels.forEach((label, index) => {
    if (labels.length > 10 && index % 3 !== 0 && index !== labels.length - 1) return;
    const x = padding.left + xStep * index;
    ctx.fillText(label.slice(5), x - 16, height - 16);
  });
}

function drawChartLegend(ctx) {
  const items = [
    ["接收", "#0f766e"],
    ["输出", "#2563eb"],
    ["模型请求", "#b45309"],
  ];
  let x = 72;
  for (const [label, color] of items) {
    ctx.fillStyle = color;
    ctx.fillRect(x, 12, 18, 4);
    ctx.fillStyle = "#1f2937";
    ctx.fillText(label, x + 24, 17);
    x += 96;
  }
}

async function toggleConversation(button) {
  button.disabled = true;
  const { botQq, scopeType, scopeId, enabled } = button.dataset;
  await fetch(conversationApiPath({ botQq, scopeType, scopeId }, "/enabled"), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: enabled === "1" }),
  });
  await refreshAll();
}

async function toggleLearning(button) {
  button.disabled = true;
  const { botQq, scopeType, scopeId, learningEnabled } = button.dataset;
  await fetch(conversationApiPath({ botQq, scopeType, scopeId }, "/learning"), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ learning_enabled: learningEnabled === "1" }),
  });
  await refreshAll();
}

async function saveReplyConfig(control) {
  const row = control.closest("tr");
  const modeSelect = row.querySelector(".mode-select");
  const prefixInput = row.querySelector(".prefix-input");
  const { botQq, scopeType, scopeId } = control.dataset;
  const mode = modeSelect ? modeSelect.value : "all";
  const prefix = prefixInput ? prefixInput.value.trim() || "/bot" : "/bot";
  const cooldownInput = row.querySelector("[data-rate-field='reply_cooldown_seconds']");
  const probabilityInput = row.querySelector("[data-rate-field='reply_probability_percent']");
  const hourlyLimitInput = row.querySelector("[data-rate-field='hourly_reply_limit']");
  const cooldown = Math.max(0, Number.parseInt(cooldownInput?.value || "0", 10) || 0);
  const probability = Math.min(1, Math.max(0, (Number.parseFloat(probabilityInput?.value || "100") || 0) / 100));
  const hourlyLimit = Math.max(0, Number.parseInt(hourlyLimitInput?.value || "0", 10) || 0);

  if (prefixInput) prefixInput.disabled = mode !== "prefix";
  await fetch(conversationApiPath({ botQq, scopeType, scopeId }, "/reply-config"), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      response_mode: mode,
      trigger_prefix: prefix,
      reply_cooldown_seconds: cooldown,
      reply_probability: probability,
      hourly_reply_limit: hourlyLimit,
    }),
  });
  await refreshAll();
}

async function openPersonaEditor(button) {
  const { botQq, scopeType, scopeId, displayName } = button.dataset;
  activeConversation = { botQq, scopeType, scopeId };
  const dialog = document.querySelector("#personaDialog");
  const notice = document.querySelector("#personaNotice");
  notice.textContent = "加载中";
  dialog.hidden = false;

  document.querySelector("#personaTitle").textContent =
    `${scopeLabel(scopeType)}：${displayName || scopeId}`;

  const response = await fetch(conversationApiPath(activeConversation));
  if (!response.ok) {
    notice.textContent = "加载失败";
    return;
  }

  const data = await response.json();
  const managerMemory = data.memory?.manager_memory || [];
  const learningEnabled = Number(data.conversation?.learning_enabled) === 1;
  document.querySelector("#personaText").value = text(data.conversation?.persona, "");
  document.querySelector("#managerMemoryText").value = managerMemory.join("\n");
  document.querySelector("#learningBatchSize").value = text(data.conversation?.learning_batch_size, "0");
  document.querySelector("#learnedMemoryPreview").textContent = formatLearnedMemory(data.memory);
  document.querySelector("#learningState").textContent = learningEnabled ? "已开启" : "已关闭";
  document.querySelector("#learningState").dataset.learningEnabled = learningEnabled ? "1" : "0";
  document.querySelector("#updateLearnedMemory").disabled = !learningEnabled;
  await loadConversationDebug();
  notice.textContent = "";
}

function closePersonaEditor() {
  activeConversation = null;
  document.querySelector("#personaDialog").hidden = true;
  document.querySelector("#personaNotice").textContent = "";
  document.querySelector("#debugNotice").textContent = "";
  document.querySelector("#debugTestText").value = "";
  document.querySelector("#debugReplyPreview").textContent = "";
  document.querySelector("#learningBatchSize").value = "0";
}

function formatLearnedMemory(memory) {
  const learned = memory?.learned_memory || {};
  const lines = [];
  if (learned.summary) lines.push(`摘要：${learned.summary}`);
  if (learned.tone) lines.push(`语气：${learned.tone}`);
  if (learned.topics?.length) lines.push(`话题：${learned.topics.join("、")}`);
  if (learned.phrases?.length) lines.push(`表达：${learned.phrases.join("、")}`);
  if (learned.avoid?.length) lines.push(`避免：${learned.avoid.join("、")}`);
  lines.push(`待学习消息数：${memory?.pending_message_count || 0}`);
  return lines.join("\n");
}

async function savePersonaEditor() {
  if (!activeConversation) return;

  const notice = document.querySelector("#personaNotice");
  const saveButton = document.querySelector("#savePersona");
  const persona = document.querySelector("#personaText").value;
  const managerMemoryText = document.querySelector("#managerMemoryText").value;
  const learningBatchSize = Math.max(
    0,
    Number.parseInt(document.querySelector("#learningBatchSize").value || "0", 10) || 0
  );
  if (learningBatchSize > 0 && learningBatchSize < 10) {
    notice.textContent = "学习批量至少 10 条；填 0 表示使用全局";
    return;
  }
  const learningEnabled = document.querySelector("#learningState").dataset.learningEnabled === "1";

  saveButton.disabled = true;
  notice.textContent = "保存中";
  try {
    const personaResponse = await fetch(conversationApiPath(activeConversation, "/persona"), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ persona }),
    });
    if (!personaResponse.ok) throw new Error(await personaResponse.text());

    const memoryResponse = await fetch(conversationApiPath(activeConversation, "/memory"), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ manager_memory_text: managerMemoryText }),
    });
    if (!memoryResponse.ok) throw new Error(await memoryResponse.text());

    const learningResponse = await fetch(conversationApiPath(activeConversation, "/learning"), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        learning_enabled: learningEnabled,
        learning_batch_size: learningBatchSize,
      }),
    });
    if (!learningResponse.ok) throw new Error(await learningResponse.text());

    notice.textContent = "已保存";
    await refreshConversations();
  } catch {
    notice.textContent = "保存失败";
  } finally {
    saveButton.disabled = false;
  }
}

async function updateLearnedMemory() {
  if (!activeConversation) return;

  const notice = document.querySelector("#personaNotice");
  const button = document.querySelector("#updateLearnedMemory");
  if (button.disabled) {
    notice.textContent = "当前会话已关闭学习";
    return;
  }
  button.disabled = true;
  notice.textContent = "学习记忆更新中";
  try {
    const response = await fetch(conversationApiPath(activeConversation, "/learned-memory/update"), {
      method: "POST",
    });
    if (!response.ok) {
      let message = "学习记忆更新失败";
      try {
        const errorData = await response.json();
        message = errorData.detail || message;
      } catch {
        message = await response.text();
      }
      throw new Error(message);
    }

    const data = await response.json();
    document.querySelector("#learnedMemoryPreview").textContent = formatLearnedMemory(data.memory);
    notice.textContent = "学习记忆已更新";
    await refreshLogs();
  } catch (error) {
    notice.textContent = `学习记忆更新失败：${error.message || "详情看后台日志"}`;
  } finally {
    button.disabled = false;
  }
}

async function clearLearningPending() {
  if (!activeConversation) return;

  const notice = document.querySelector("#personaNotice");
  const button = document.querySelector("#clearLearningPending");

  button.disabled = true;
  notice.textContent = "清空中";
  try {
    const response = await fetch(conversationApiPath(activeConversation, "/learned-memory/clear-pending"), {
      method: "POST",
    });
    if (!response.ok) throw new Error(await readErrorMessage(response, "清空失败"));

    const data = await response.json();
    document.querySelector("#learnedMemoryPreview").textContent = formatLearnedMemory(data.memory);
    notice.textContent = "待学习消息数已清空";
    await refreshLogs();
  } catch (error) {
    notice.textContent = `清空失败：${error.message || "详情看后台日志"}`;
  } finally {
    button.disabled = false;
  }
}

function formatRecentMessages(items) {
  if (!items?.length) return "暂无消息";
  return items
    .map((item) => {
      const speaker = Number(item.is_bot) === 1 ? "机器人" : `用户${item.user_id}`;
      return `[${text(item.created_at, "-")}] ${speaker}: ${text(item.text, "")}`;
    })
    .join("\n");
}

function formatPromptMessages(messages) {
  if (!messages?.length) return "暂无提示词";
  return messages
    .map((item, index) => `#${index + 1} ${item.role}\n${text(item.content, "")}`)
    .join("\n\n");
}

async function loadConversationDebug() {
  if (!activeConversation) return;

  const recentPreview = document.querySelector("#recentMessagesPreview");
  const promptPreview = document.querySelector("#promptPreview");
  const notice = document.querySelector("#debugNotice");

  recentPreview.textContent = "加载中";
  promptPreview.textContent = "加载中";
  try {
    const response = await fetch(conversationApiPath(activeConversation, "/debug"));
    if (!response.ok) throw new Error(await response.text());

    const data = await response.json();
    recentPreview.textContent = formatRecentMessages(data.recent_messages);
    promptPreview.textContent = formatPromptMessages(data.prompt_messages);
    notice.textContent = "";
  } catch {
    recentPreview.textContent = "加载失败";
    promptPreview.textContent = "加载失败";
    notice.textContent = "调试信息加载失败";
  }
}

async function runDebugReply() {
  if (!activeConversation) return;

  const notice = document.querySelector("#debugNotice");
  const button = document.querySelector("#runDebugReply");
  const testText = document.querySelector("#debugTestText").value;
  const replyPreview = document.querySelector("#debugReplyPreview");

  button.disabled = true;
  notice.textContent = "生成中";
  replyPreview.textContent = "";
  try {
    const response = await fetch(conversationApiPath(activeConversation, "/debug-reply"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ test_text: testText }),
    });
    if (!response.ok) {
      let message = "生成失败";
      try {
        const errorData = await response.json();
        message = errorData.detail || message;
      } catch {
        message = await response.text();
      }
      throw new Error(message);
    }

    const data = await response.json();
    replyPreview.textContent = data.reply || "";
    document.querySelector("#promptPreview").textContent = formatPromptMessages(data.prompt_messages);
    notice.textContent = "已生成，不会发送到 QQ";
    await refreshLogs();
  } catch (error) {
    notice.textContent = `生成失败：${error.message || "详情看后台日志"}`;
  } finally {
    button.disabled = false;
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const notice = document.querySelector("#settingsNotice");
  const submitButton = form.querySelector("button[type='submit']");
  const formData = new FormData(form);

  submitButton.disabled = true;
  notice.textContent = "保存中";
  try {
    const response = await fetch("/api/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        llm_base_url: text(formData.get("llm_base_url"), ""),
        llm_api_key: text(formData.get("llm_api_key"), ""),
        llm_model: text(formData.get("llm_model"), ""),
        llm_temperature: text(formData.get("llm_temperature"), ""),
        llm_max_tokens: text(formData.get("llm_max_tokens"), ""),
        bot_manager_qqs: text(formData.get("bot_manager_qqs"), ""),
        bot_memory_batch_size: text(formData.get("bot_memory_batch_size"), "40"),
        bot_global_system_prompt: text(formData.get("bot_global_system_prompt"), ""),
      }),
    });
    if (!response.ok) {
      throw new Error(await response.text());
    }
    const data = await response.json();
    form.querySelector("[name='llm_api_key']").value = "";
    form.elements.llm_api_key.placeholder = data.api_key_updated
      ? "API Key 已保存，留空不修改"
      : form.elements.llm_api_key.placeholder;
    notice.textContent = data.api_key_updated ? "已保存，API Key 已更新" : "已保存";
  } catch {
    notice.textContent = "保存失败";
  } finally {
    submitButton.disabled = false;
  }
}

async function refreshAll() {
  await refreshStatus();
  await refreshSettings();
  await refreshCommands();
  await refreshConversations();
  await refreshStats();
  await refreshLogs();
  await refreshBackups();
}

document.querySelector("#refreshStatus").addEventListener("click", refreshAll);
document.querySelector("#testModel").addEventListener("click", testModelConnection);
document.querySelector("#refreshLogs").addEventListener("click", refreshLogs);
document.querySelector("#refreshCommands").addEventListener("click", () => refreshCommands(true));
document.querySelector("#saveCommands").addEventListener("click", saveCommands);
document.querySelector("#toggleCommandList").addEventListener("click", toggleCommandList);
document.querySelector("#addCommand").addEventListener("click", addCommand);
document.querySelector("#commandList").addEventListener("click", (event) => {
  const deleteButton = event.target.closest(".delete-command-button");
  if (deleteButton) {
    deleteCommand(deleteButton);
  }
});
document.querySelector("#refreshBackups").addEventListener("click", refreshBackups);
document.querySelector("#createBackup").addEventListener("click", createBackup);
document.querySelector("#refreshStats").addEventListener("click", refreshStats);
document.querySelector("#statsDays").addEventListener("change", refreshStats);
document.querySelector("#backupList").addEventListener("click", (event) => {
  const inspectButton = event.target.closest(".backup-inspect-button");
  if (inspectButton) {
    inspectBackup(inspectButton);
    return;
  }

  const restoreButton = event.target.closest(".backup-restore-button");
  if (restoreButton) {
    restoreBackup(restoreButton);
  }
});
document.querySelector("#settingsForm").addEventListener("submit", saveSettings);
document.querySelector("#closePersona").addEventListener("click", closePersonaEditor);
document.querySelector("#cancelPersona").addEventListener("click", closePersonaEditor);
document.querySelector("#savePersona").addEventListener("click", savePersonaEditor);
document.querySelector("#updateLearnedMemory").addEventListener("click", updateLearnedMemory);
document.querySelector("#clearLearningPending").addEventListener("click", clearLearningPending);
document.querySelector("#refreshDebug").addEventListener("click", loadConversationDebug);
document.querySelector("#runDebugReply").addEventListener("click", runDebugReply);
document.querySelector("#conversationList").addEventListener("click", (event) => {
  const personaButton = event.target.closest(".persona-button");
  if (personaButton) {
    openPersonaEditor(personaButton);
    return;
  }

  const button = event.target.closest(".toggle-button");
  if (button) {
    toggleConversation(button).catch(refreshAll);
    return;
  }

  const learningButton = event.target.closest(".learning-button");
  if (learningButton) {
    toggleLearning(learningButton).catch(refreshAll);
  }
});
document.querySelector("#conversationList").addEventListener("change", (event) => {
  const control = event.target.closest(".mode-select, .prefix-input, .rate-input");
  if (control) {
    saveReplyConfig(control).catch(refreshAll);
  }
});
refreshAll().catch(() => {
  const firstValue = document.querySelector("#statusGrid dd");
  if (firstValue) {
    firstValue.textContent = "异常";
  }
});
