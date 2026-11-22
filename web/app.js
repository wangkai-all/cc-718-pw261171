"use strict";

// ---------------------------------------------------------------------------
// state
// ---------------------------------------------------------------------------
const state = {
  units: [],
  categories: {},
  datasets: [],
  datasetId: null,
  fields: [],
  fx: {},
  output: null,
  analysis: null,
  analysisByField: {},
  latest: null,
  versions: [],
  selectedRow: 0,
  selectedField: null,
  preview: null,
  dirty: false,
  busy: false,
  dataView: null,       // output payload currently rendered
  dataVersion: null,
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null) continue;
    if (key === "class") node.className = value;
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (value === true) node.setAttribute(key, "");
    else if (value !== false) node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtNum(value) {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  if (Number.isInteger(n)) return n.toLocaleString("zh-CN");
  const abs = Math.abs(n);
  if (abs !== 0 && abs < 1e-4) return n.toExponential(3);
  return n.toLocaleString("zh-CN", { maximumFractionDigits: 4 });
}

function unitLabel(symbol) {
  if (!symbol) return "";
  const unit = state.units.find((u) => u.symbol === symbol);
  return unit ? unit.label : symbol;
}

function toast(message, kind = "") {
  const node = $("#toast");
  node.textContent = message;
  node.className = "toast " + kind;
  node.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { node.hidden = true; }, 3600);
}

async function api(path, options = {}) {
  const opts = { headers: { "Content-Type": "application/json" }, ...options };
  if (opts.body && typeof opts.body !== "string") opts.body = JSON.stringify(opts.body);
  const response = await fetch(path, opts);
  let payload = null;
  try { payload = await response.json(); } catch { payload = null; }
  if (!response.ok && !payload) throw new Error(`HTTP ${response.status}`);
  return { ok: response.ok, status: response.status, data: payload };
}

// ---------------------------------------------------------------------------
// derived helper: names referenced by an expression
// ---------------------------------------------------------------------------
function expressionRefs(expression) {
  const out = new Set();
  if (!expression) return out;
  const stripped = expression
    .replace(/\[[^\]]*\]/g, " ")
    .replace(/"[^"]*"/g, " ");
  for (const m of stripped.matchAll(/[A-Za-z_°][A-Za-z0-9_°]*/g)) {
    const word = m[0];
    if (/^(IF|THEN|ELSE|AND|OR|NOT|SUM|TO|TRUE|FALSE)$/i.test(word)) continue;
    out.add(word);
  }
  return out;
}

function derivedNames() {
  return new Set(state.fields.filter((f) => f.kind === "derived").map((f) => f.name));
}

function dependentsOf(name) {
  const derived = derivedNames();
  const result = [];
  const stack = [name];
  const seen = new Set();
  while (stack.length) {
    const cur = stack.pop();
    for (const field of state.fields) {
      if (field.kind !== "derived" || seen.has(field.name)) continue;
      if (expressionRefs(field.expression).has(cur)) {
        seen.add(field.name);
        result.push(field.name);
        stack.push(field.name);
      }
    }
  }
  return result;
}

// ---------------------------------------------------------------------------
// analysis
// ---------------------------------------------------------------------------
async function runAnalysis() {
  const { ok, data } = await api(`/api/datasets/${state.datasetId}/analyze`, {
    method: "POST",
    body: { fields: state.fields, fx: state.fx },
  });
  if (!ok || !data) return;
  state.analysis = data;
  state.analysisByField = {};
  for (const item of data.analysis || []) state.analysisByField[item.name] = item;
  renderErrors();
  renderDerivedList();
  renderInspector();
  const hasErrors = (data.errors || []).length > 0;
  const dot = $("#analysis-status");
  dot.className = "status-dot " + (hasErrors ? "bad" : "ok");
  $("#inspector-sub").textContent = hasErrors
    ? `${data.errors.length} 个问题阻止发布`
    : "静态检查通过，可预览差异";
}

// ---------------------------------------------------------------------------
// raw fields
// ---------------------------------------------------------------------------
function rawFields() { return state.fields.filter((f) => f.kind === "raw"); }

function renderRawTable() {
  const tbody = $("#raw-table tbody");
  tbody.innerHTML = "";
  $("#raw-count").textContent = `${rawFields().length} 个原始字段`;
  const categories = {};
  for (const unit of state.units) {
    (categories[unit.category] ||= []).push(unit);
  }
  for (const field of rawFields()) {
    const unit = state.units.find((u) => u.symbol === field.unit);
    const row = el("tr");
    row.appendChild(el("td", { class: "mono" }, field.name));
    const labelCell = el("td");
    const labelInput = el("input", {
      value: field.label || "",
      placeholder: "业务含义",
      oninput: (e) => { field.label = e.target.value; markDirty(); },
    });
    labelCell.appendChild(labelInput);
    row.appendChild(labelCell);

    const unitCell = el("td");
    const select = el("select", { class: "unit-select" });
    select.appendChild(el("option", { value: "" }, "— 未登记 —"));
    for (const [cat, list] of Object.entries(categories)) {
      const group = el("optgroup", { label: state.categories[cat] || cat });
      for (const u of list) {
        group.appendChild(el("option", { value: u.symbol }, `${u.symbol} · ${u.label}`));
      }
      select.appendChild(group);
    }
    select.value = field.unit || "";
    select.addEventListener("change", () => {
      field.unit = select.value;
      markDirty();
      renderRawTable();
      scheduleAnalysis();
    });
    unitCell.appendChild(select);
    row.appendChild(unitCell);

    const dim = el("td", { class: "mono" },
      unit ? `${unit.dimension}` : el("span", { class: "eq-note" }, "未知"));
    if (unit && unit.is_affine) {
      dim.appendChild(el("span", { class: "type-pill temp", style: "margin-left:6px" }, "带偏移"));
    }
    row.appendChild(dim);
    tbody.appendChild(row);
  }
  // Avoid the browser opening the first select's popup on initial focus.
  if (document.activeElement && document.activeElement.tagName === "SELECT") {
    document.activeElement.blur();
  }
}

function renderFx() {
  $("#fx-usd-cny").value = state.fx["USD>CNY"] ?? "";
}

function readFx() {
  const value = $("#fx-usd-cny").value.trim();
  const fx = {};
  if (value) fx["USD>CNY"] = Number(value);
  return fx;
}

function markDirty() {
  state.dirty = true;
  $("#version-chip").classList.add("stale");
  $("#version-chip").textContent = "未发布修改";
}

// ---------------------------------------------------------------------------
// derived fields
// ---------------------------------------------------------------------------
function renderDerivedList() {
  const list = $("#derived-list");
  list.innerHTML = "";
  const derived = state.fields.filter((f) => f.kind === "derived");
  if (!derived.length) {
    list.appendChild(el("div", { class: "inspector-empty" },
      "还没有派生字段。点击右上角新增，或导入 CSV 后开始登记。"));
    return;
  }
  for (const field of derived) {
    const info = state.analysisByField[field.name] || {};
    const status = info.status || "unknown";
    const card = el("div", {
      class: "card " + (status === "error" ? "invalid" : status === "blocked" ? "blocked" : ""),
      dataset: { field: field.name },
    });
    const head = el("div", { class: "card-head" });
    head.appendChild(el("div", { class: "card-title" },
      el("input", {
        value: field.name, spellcheck: "false",
        onchange: (e) => renameField(field, e.target.value.trim()),
      })));
    if (status === "ok") {
      const type = info.type || {};
      head.appendChild(el("span", { class: "type-pill" + typeClass(type) },
        `${type.symbol || "?"} · ${type.kind_label || ""}`));
      if (info.aggregate) head.appendChild(el("span", { class: "type-pill agg" }, "聚合"));
    } else if (status === "blocked") {
      head.appendChild(el("span", { class: "type-pill bad" }, "被上游阻塞"));
    } else if (status === "error") {
      head.appendChild(el("span", { class: "type-pill bad" }, "单位错误"));
    }
    const actions = el("div", { class: "card-actions" });
    actions.appendChild(el("button", {
      class: "icon-btn", title: "删除字段", onclick: () => removeField(field),
    }, "×"));
    head.appendChild(actions);
    card.appendChild(head);

    const grid = el("div", { class: "card-grid" });
    const exprLabel = el("label", {}, "表达式");
    exprLabel.appendChild(el("input", {
      class: "expr", value: field.expression || "", spellcheck: "false",
      placeholder: '例如 distance / duration',
      oninput: (e) => { field.expression = e.target.value; markDirty(); scheduleAnalysis(); },
    }));
    grid.appendChild(exprLabel);

    const outLabel = el("label", {}, "输出单位");
    const outSelect = el("select", {});
    outSelect.appendChild(el("option", { value: "" }, "auto"));
    const cats = {};
    for (const u of state.units) (cats[u.category] ||= []).push(u);
    for (const [cat, units] of Object.entries(cats)) {
      const group = el("optgroup", { label: state.categories[cat] || cat });
      for (const u of units) group.appendChild(el("option", { value: u.symbol }, u.symbol));
      outSelect.appendChild(group);
    }
    outSelect.value = field.output_unit || "";
    outSelect.addEventListener("change", () => {
      field.output_unit = outSelect.value || null;
      markDirty(); scheduleAnalysis();
    });
    outLabel.appendChild(outSelect);
    grid.appendChild(outLabel);

    const labelLabel = el("label", {}, "业务含义");
    labelLabel.appendChild(el("input", {
      value: field.label || "", placeholder: "可选",
      oninput: (e) => { field.label = e.target.value; markDirty(); },
    }));
    grid.appendChild(labelLabel);
    card.appendChild(grid);

    const result = el("div", { class: "card-result" });
    if (status === "ok") {
      const type = info.type || {};
      result.appendChild(el("span", {}, "推导："));
      result.appendChild(el("span", { class: "type-pill" + typeClass(type) },
        `${type.symbol} · ${type.dimension}`));
      if (field.output_unit && field.output_unit !== type.symbol) {
        result.appendChild(el("span", { class: "eq-note" },
          `→ 输出 ${field.output_unit}`));
      }
      if ((info.references || []).length) {
        result.appendChild(el("span", { class: "eq-note" },
          `引用 ${info.references.join(", ")}`));
      }
    } else if (status === "blocked") {
      result.appendChild(el("span", { class: "type-pill bad" }, "上游字段 " +
        (info.blocked_by || []).join(", ") + " 未通过，无法推导"));
    } else if (info.error) {
      result.appendChild(el("span", { class: "type-pill bad" }, info.error.code));
      result.appendChild(el("span", { class: "eq-note" }, info.error.message));
    }
    card.appendChild(result);
    list.appendChild(card);
  }
}

function typeClass(type) {
  if (!type) return "";
  if (type.kind === "money") return " money";
  if (type.kind === "ratio" || type.kind === "number") return " ratio";
  if (type.kind && type.kind.startsWith("temperature")) return " temp";
  return "";
}

function addDerived() {
  let index = 1;
  let name = `new_field_${index}`;
  const names = new Set(state.fields.map((f) => f.name));
  while (names.has(name)) name = `new_field_${++index}`;
  state.fields.push({ name, kind: "derived", expression: "", output_unit: null, label: "" });
  markDirty();
  renderDerivedList();
  scheduleAnalysis();
}

function removeField(field) {
  state.fields = state.fields.filter((f) => f !== field);
  markDirty();
  renderDerivedList();
  scheduleAnalysis();
}

function renameField(field, newName) {
  if (!newName || newName === field.name) return;
  if (state.fields.some((f) => f.name === newName)) {
    toast(`字段名 ${newName} 已存在`, "bad");
    renderDerivedList();
    return;
  }
  const old = field.name;
  field.name = newName;
  for (const other of state.fields) {
    if (other.kind === "derived" && other.expression) {
      other.expression = other.expression.replace(new RegExp(`\\b${old}\\b`, "g"), newName);
    }
  }
  markDirty();
  renderDerivedList();
  scheduleAnalysis();
}

// ---------------------------------------------------------------------------
// errors + inspector
// ---------------------------------------------------------------------------
function renderErrors() {
  const wrap = $("#error-list");
  wrap.innerHTML = "";
  const errors = (state.analysis && state.analysis.errors) || [];
  if (!errors.length) return;
  for (const err of errors) {
    const box = el("div", { class: "err" });
    box.appendChild(el("span", { class: "code" }, err.code));
    box.appendChild(el("span", { class: "msg" }, err.message));
    if (err.field) {
      box.appendChild(el("div", { class: "meta" }, `字段：${err.field}`));
    }
    if (err.expression) {
      box.appendChild(el("div", { class: "meta" }, `表达式：${err.expression}`));
    }
    if (err.path) box.appendChild(el("div", { class: "meta" }, `位置：${err.path}`));
    if (err.details && Object.keys(err.details).length) {
      const details = Object.entries(err.details)
        .filter(([k]) => !["left", "right", "from", "to"].includes(k))
        .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`);
      if (details.length) box.appendChild(el("div", { class: "meta" }, details.join(" · ")));
    }
    if ((err.affected_downstream || []).length) {
      box.appendChild(el("div", { class: "affect" },
        `影响下游：${err.affected_downstream.join(" → ")}`));
    }
    wrap.appendChild(box);
  }
}

function renderInspector() {
  const wrap = $("#inspector");
  wrap.innerHTML = "";
  const derived = state.fields.filter((f) => f.kind === "derived");
  if (!derived.length) {
    wrap.appendChild(el("div", { class: "inspector-empty" }, "暂无派生字段。"));
    return;
  }
  for (const field of derived) {
    const info = state.analysisByField[field.name] || {};
    const item = el("div", {
      class: "insp-field" + (state.selectedField === field.name ? " sel" : ""),
      onclick: () => { state.selectedField = field.name; renderInspector(); renderDataTable(); },
    });
    const top = el("div", { class: "insp-top" });
    top.appendChild(el("span", { class: "insp-name" }, field.name));
    if (info.status === "ok") {
      const type = info.type || {};
      top.appendChild(el("span", { class: "type-pill" + typeClass(type) }, type.symbol || "?"));
      if (info.aggregate) top.appendChild(el("span", { class: "type-pill agg" }, "聚合"));
    } else if (info.status === "blocked") {
      top.appendChild(el("span", { class: "type-pill bad" }, "阻塞"));
    } else if (info.status === "error") {
      top.appendChild(el("span", { class: "type-pill bad" }, "错误"));
    }
    item.appendChild(top);
    item.appendChild(el("div", { class: "insp-expr" }, field.expression || "（空表达式）"));
    if (info.status === "ok" && info.type) {
      item.appendChild(el("div", { class: "insp-deps" },
        `量纲 ${info.type.dimension}${field.output_unit ? ` · 输出 ${field.output_unit}` : ""}`));
    }
    const deps = dependentsOf(field.name);
    if (deps.length) {
      item.appendChild(el("div", { class: "insp-deps" },
        "下游：", el("b", {}, deps.join(" → "))));
    }
    if (info.status === "blocked" && (info.blocked_by || []).length) {
      item.appendChild(el("div", { class: "insp-deps" },
        "等待上游：", el("b", {}, info.blocked_by.join(", "))));
    }
    wrap.appendChild(item);
  }
}

let analysisTimer = null;
function scheduleAnalysis() {
  clearTimeout(analysisTimer);
  analysisTimer = setTimeout(() => { runAnalysis(); }, 220);
}

// ---------------------------------------------------------------------------
// preview
// ---------------------------------------------------------------------------
async function previewChanges() {
  if (!state.datasetId) return;
  setBusy(true);
  const { ok, data } = await api(`/api/datasets/${state.datasetId}/preview`, {
    method: "POST",
    body: {
      fields: state.fields,
      fx: state.fx,
      output: state.output,
      base_version: state.latest ? state.latest.version : null,
    },
  });
  setBusy(false);
  if (!ok || !data) { toast("预览失败", "bad"); return; }
  state.preview = data;
  renderPreview(data);
  if (!data.valid) {
    toast(`预览发现 ${data.errors.length} 个问题，未修改任何版本`, "bad");
  } else {
    toast("预览完成：确认后再发布", "good");
  }
}

function renderPreview(data) {
  const box = $("#preview-box");
  box.hidden = false;
  box.innerHTML = "";
  box.appendChild(el("h3", {}, `预览${data.base_version ? `（对比 v${data.base_version}）` : ""}`));
  const diff = data.diff || {};
  if ((diff.added || []).length || (diff.removed || []).length || (diff.changed || []).length) {
    const group = el("div", { class: "diff-group" });
    group.appendChild(el("h4", {}, "规则差异"));
    for (const name of diff.added || []) {
      group.appendChild(el("span", { class: "chip add" }, `+ ${name}`));
    }
    for (const name of diff.removed || []) {
      group.appendChild(el("span", { class: "chip rem" }, `− ${name}`));
    }
    for (const change of diff.changed || []) {
      group.appendChild(el("span", { class: "chip chg" }, `~ ${change.name}`));
      const bits = [];
      if (change.unit_from !== change.unit_to) {
        bits.push(`unit ${change.unit_from || "—"} → ${change.unit_to || "—"}`);
      }
      if (change.expression_from !== change.expression_to) {
        bits.push(`expr ${change.expression_from || "—"} → ${change.expression_to || "—"}`);
      }
      if (change.output_unit_from !== change.output_unit_to) {
        bits.push(`output ${change.output_unit_from || "—"} → ${change.output_unit_to || "—"}`);
      }
      group.appendChild(el("div", { class: "diff-row" },
        el("b", {}, change.name), "：", bits.join("；")));
    }
    box.appendChild(group);
  } else {
    box.appendChild(el("div", { class: "diff-row" }, "规则与当前成功版本一致。"));
  }
  const dataDiff = data.data_diff || {};
  const group = el("div", { class: "diff-group" });
  group.appendChild(el("h4", {}, "数据差异"));
  if (!data.valid) {
    group.appendChild(el("div", { class: "diff-row" }, "存在错误，无法生成预览数据。"));
    for (const err of data.errors) {
      group.appendChild(el("div", { class: "diff-row" },
        el("b", {}, err.code), " ", err.message,
        err.field ? `（${err.field}）` : ""));
    }
  } else if (!dataDiff.available) {
    group.appendChild(el("div", { class: "diff-row" }, "暂无历史成功版本可对比。"));
  } else if (!(dataDiff.changes || []).length) {
    group.appendChild(el("div", { class: "diff-row" }, "输出数值没有变化。"));
  } else {
    for (const change of dataDiff.changes || []) {
      const unitNote = change.unit_from !== change.unit_to
        ? ` · 单位 ${change.unit_from || "—"} → ${change.unit_to || "—"}` : "";
      const row = el("div", { class: "diff-row" },
        el("b", {}, change.field), ` · ${change.kind === "added" ? "新增" : change.kind === "removed" ? "移除" : "数值变化"}`
        + ` · ${change.affected_rows} 行${unitNote}`);
      for (const sample of change.samples || []) {
        row.appendChild(el("div", { class: "sample-line" },
          `第 ${sample.row} 行：${fmtNum(sample.from)} → ${fmtNum(sample.to)}`));
      }
      group.appendChild(row);
    }
  }
  box.appendChild(group);
}

// ---------------------------------------------------------------------------
// publish
// ---------------------------------------------------------------------------
async function publish() {
  if (!state.datasetId) return;
  setBusy(true);
  state.fx = readFx();
  const { ok, data } = await api(`/api/datasets/${state.datasetId}/publish`, {
    method: "POST",
    body: { fields: state.fields, fx: state.fx, output: state.output },
  });
  setBusy(false);
  if (!data) { toast("发布请求失败", "bad"); return; }
  const version = data.version;
  if (version.status === "success") {
    state.dirty = false;
    state.preview = null;
    $("#preview-box").hidden = true;
    $("#version-chip").classList.remove("stale");
    $("#version-chip").textContent = `v${version.version} · 已发布`;
    toast(`版本 v${version.version} 发布成功`, "good");
    showBanner("");
    await refreshLatest();
    await loadVersions();
  } else {
    const items = (version.error && version.error.items) || [];
    const first = items[0];
    const stale = state.latest;
    showBanner(
      `发布失败（v${version.version}），继续读取上一成功版本`
      + (stale ? ` v${stale.version}` : "")
      + (first ? `：${first.message}` : ""),
      true,
    );
    toast("发布被拒绝，旧版数据保持不变", "bad");
    renderPublishErrors(items);
    await loadVersions();
  }
}

function renderPublishErrors(items) {
  const wrap = $("#error-list");
  wrap.innerHTML = "";
  for (const err of items) {
    const box = el("div", { class: "err" });
    box.appendChild(el("span", { class: "code" }, err.code));
    box.appendChild(el("span", { class: "msg" }, err.message));
    if (err.field) box.appendChild(el("div", { class: "meta" }, `字段：${err.field}`));
    if (err.expression) box.appendChild(el("div", { class: "meta" }, `表达式：${err.expression}`));
    if ((err.affected_downstream || []).length) {
      box.appendChild(el("div", { class: "affect" },
        `影响下游：${err.affected_downstream.join(" → ")}`));
    }
    wrap.appendChild(box);
  }
}

function showBanner(message, isError = false) {
  const banner = $("#banner");
  if (!message) { banner.hidden = true; banner.textContent = ""; return; }
  banner.hidden = false;
  banner.className = "banner" + (isError ? "" : " info");
  banner.textContent = message;
}

function setBusy(busy) {
  state.busy = busy;
  for (const id of ["#btn-publish", "#btn-preview", "#btn-add-derived"]) {
    const node = $(id);
    if (node) node.disabled = busy;
  }
}

// ---------------------------------------------------------------------------
// data table
// ---------------------------------------------------------------------------
function showData(outputs, versionNumber) {
  state.dataView = outputs;
  state.dataVersion = versionNumber;
  if (state.selectedRow >= (outputs.rows || []).length) state.selectedRow = 0;
  renderDataTable();
  renderRowSteps();
}

function renderDataTable() {
  const table = $("#data-table");
  table.innerHTML = "";
  const data = state.dataView;
  if (!data || !(data.columns || []).length) {
    table.appendChild(el("tbody", {},
      el("tr", {}, el("td", { class: "null" }, "暂无已发布数据。完善规则后发布新版本。"))));
    return;
  }
  const fieldInfo = Object.fromEntries(state.fields.map((f) => [f.name, f]));
  const rowCols = data.columns.filter((c) => c.kind !== "aggregate");
  const aggCols = data.columns.filter((c) => c.kind === "aggregate");
  const head = el("tr");
  head.appendChild(el("th", {}, "行"));
  for (const col of rowCols) {
    const info = fieldInfo[col.name] || {};
    const cls = col.kind === "raw" ? "raw" : "derived";
    const th = el("th", { class: cls }, info.label || col.name);
    th.appendChild(el("span", { class: "unit-tag" }, col.unit || (info.unit || "")));
    head.appendChild(th);
  }
  if (aggCols.length) {
    const th = el("th", { class: "agg agg-group" }, "聚合（数据集整体）");
    th.colSpan = aggCols.length;
    head.appendChild(th);
  }
  table.appendChild(el("thead", {}, head));
  const tbody = el("tbody");
  for (const [index, row] of (data.rows || []).entries()) {
    const tr = el("tr", {
      class: "clickable" + (state.selectedRow === index ? " sel" : ""),
      onclick: () => {
        state.selectedRow = index;
        renderDataTable();
        renderRowSteps();
      },
    });
    tr.appendChild(el("td", { class: "mono" }, index + 1));
    for (const col of rowCols) {
      const value = row[col.name];
      const td = el("td", { class: value === null || value === undefined ? "null" : "" },
        value === null || value === undefined ? "—" : fmtNum(value));
      tr.appendChild(td);
    }
    for (const col of aggCols) {
      tr.appendChild(el("td", { class: "agg-blank" }, ""));
    }
    tbody.appendChild(tr);
  }
  if (aggCols.length && (data.rows || []).length) {
    const tr = el("tr", { class: "agg-row" });
    tr.appendChild(el("td", { class: "mono" }, "Σ"));
    for (const _ of rowCols) tr.appendChild(el("td", { class: "agg-blank" }, ""));
    for (const col of aggCols) {
      const value = (data.aggregates || {})[col.name];
      tr.appendChild(el("td", { class: "agg" },
        value === null || value === undefined ? "—" : fmtNum(value)));
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  const hint = $("#data-hint");
  hint.textContent = state.dataVersion
    ? `读取版本 v${state.dataVersion} 的输出（${(data.rows || []).length} 行）。点击任意行查看换算步骤。`
    : `处理中：${(data.rows || []).length} 行。`;
}

function renderRowSteps() {
  const box = $("#row-steps");
  const data = state.dataView;
  if (!data) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = "";
  const row = state.selectedRow;
  const steps = data.steps || {};
  const selected = state.selectedField;
  const names = Object.keys(steps);
  box.appendChild(el("h3", {},
    `第 ${row + 1} 行换算步骤` + (selected ? ` · ${selected}` : "")));
  if (!names.length) {
    box.appendChild(el("div", { class: "inspector-empty" }, "该版本没有记录换算步骤。"));
  }
  const ordered = selected && steps[selected]
    ? [selected, ...names.filter((n) => n !== selected)]
    : names;
  for (const name of ordered) {
    if (!steps[name] || steps[name].kind !== "row") continue;
    const info = steps[name];
    const field = state.fields.find((f) => f.name === name) || {};
    const chain = info.kind === "row"
      ? (info.rows && info.rows[row]) || info.rows?.[String(row)] || []
      : info.steps || [];
    const section = el("div", { style: "margin-bottom:12px" });
    section.appendChild(el("div", { class: "insp-deps" },
      el("b", {}, name), " ", field.label || "", info.unit ? ` · ${info.unit}` : ""));
    const list = el("div", { class: "step-chain" });
    if (!chain.length) {
      list.appendChild(el("div", { class: "step small" }, "无需换算（原单位即为输出单位）"));
    }
    for (const step of chain) {
      const line = el("div", { class: "step" });
      if (step.op && ["sum", "literal", "from"].includes(step.op)) {
        line.appendChild(el("span", { class: "op" }, opLabel(step.op)));
      }
      line.appendChild(el("span", { class: "val" }, step.formula || ""));
      list.appendChild(line);
    }
    section.appendChild(list);
    box.appendChild(section);
  }
  const aggregates = data.aggregates || {};
  const aggNames = Object.keys(aggregates);
  if (aggNames.length) {
    const wrap = el("div", {});
    wrap.appendChild(el("h3", { style: "margin-top:4px" }, "聚合字段（整个数据集）"));
    const strip = el("div", { class: "agg-strip" });
    for (const [name, value] of Object.entries(aggregates)) {
      const col = data.columns.find((c) => c.name === name);
      strip.appendChild(el("div", { class: "agg-card" },
        name, " = ", el("b", {}, fmtNum(value)), " ", col ? col.unit || "" : ""));
    }
    wrap.appendChild(strip);
    const detail = el("details", { class: "agg-detail" });
    detail.appendChild(el("summary", {}, "查看聚合换算过程"));
    for (const name of aggNames) {
      const info = steps[name];
      if (info && info.kind === "aggregate") {
        detail.appendChild(el("div", { class: "insp-deps", style: "margin-top:8px" },
          el("b", {}, name)));
        const list = el("div", { class: "step-chain", style: "margin-top:6px" });
        for (const step of info.steps || []) {
          list.appendChild(el("div", { class: "step" },
            el("span", { class: "val" }, step.formula || "")));
        }
        detail.appendChild(list);
      }
    }
    wrap.appendChild(detail);
    box.appendChild(wrap);
  }
}

function opLabel(op) {
  return ({ "+": "＋", "-": "－", "*": "×", "/": "÷", "^": "乘方", sum: "Σ", literal: "常量", from: "起点" })[op] || op;
}

// ---------------------------------------------------------------------------
// versions and events
// ---------------------------------------------------------------------------
async function loadVersions() {
  const { ok, data } = await api(`/api/datasets/${state.datasetId}/versions`);
  if (!ok || !data) return;
  state.versions = data.versions || [];
  const tbody = $("#version-table tbody");
  tbody.innerHTML = "";
  for (const version of state.versions) {
    const tr = el("tr");
    tr.appendChild(el("td", { class: "mono" }, `v${version.version}`));
    tr.appendChild(el("td", {}, el("span", { class: `status-pill ${version.status}` },
      version.status === "success" ? "成功" : "失败")));
    let fieldCount = "—";
    if (version.summary) {
      try {
        const summary = typeof version.summary === "string" ? JSON.parse(version.summary) : version.summary;
        if (summary.fields) fieldCount = summary.fields.length;
        else if (summary.row_count) fieldCount = summary.row_count;
      } catch { /* ignore */ }
    }
    tr.appendChild(el("td", { class: "mono" }, String(fieldCount)));
    tr.appendChild(el("td", { class: "mono" },
      version.duration_ms ? `${version.duration_ms.toFixed(1)} ms` : "—"));
    tr.appendChild(el("td", { class: "mono" },
      new Date(version.created_at * 1000).toLocaleString("zh-CN", { hour12: false })));
    const actions = el("td");
    if (version.status === "success") {
      actions.appendChild(el("button", {
        class: "btn ghost small", onclick: () => loadVersion(version.version),
      }, "读取"));
    }
    tr.appendChild(actions);
    tbody.appendChild(tr);
  }
  if (!state.versions.length) {
    tbody.appendChild(el("tr", {}, el("td", { colspan: 6, class: "null" }, "尚无发布记录")));
  }
}

async function loadVersion(number) {
  const { ok, data } = await api(`/api/datasets/${state.datasetId}/versions/${number}`);
  if (!ok || !data) { toast("读取版本失败", "bad"); return; }
  const version = data.version;
  if (version.status !== "success" || !version.outputs) {
    toast(`v${number} 是失败版本，不能读取数据`, "bad");
    return;
  }
  state.fields = version.fields;
  state.fx = version.fx || {};
  state.output = version.outputs.selected;
  state.latest = version;
  state.dirty = false;
  state.selectedRow = 0;
  $("#version-chip").classList.remove("stale");
  $("#version-chip").textContent = `v${version.version} · 读取中`;
  renderFx();
  renderRawTable();
  renderDerivedList();
  showData(version.outputs, version.version);
  $("#version-chip").textContent = `v${version.version} · 已读取`;
  showBanner("");
  await runAnalysis();
}

async function refreshLatest() {
  const { ok, data } = await api(`/api/datasets/${state.datasetId}/latest`);
  if (!ok || !data || !data.version) return;
  const version = data.version;
  state.latest = version;
  state.output = version.outputs ? version.outputs.selected : state.output;
  if (!state.dirty) {
    state.fields = version.fields;
    state.fx = version.fx || {};
    renderFx();
    renderRawTable();
    renderDerivedList();
    $("#version-chip").textContent = `v${version.version} · 已发布`;
  }
  showData(version.outputs, version.version);
  await runAnalysis();
}

async function loadEvents() {
  const { ok, data } = await api(`/api/datasets/${state.datasetId}/events`);
  if (!ok || !data) return;
  const wrap = $("#events");
  wrap.innerHTML = "";
  wrap.appendChild(el("h3", { style: "margin-bottom:8px" }, "发布事件"));
  for (const event of data.events || []) {
    const kind = event.kind === "publish_success" ? "ok"
      : event.kind === "publish_failed" ? "bad" : "";
    wrap.appendChild(el("div", { class: "event " + kind },
      el("span", { class: "when" },
        new Date(event.created_at * 1000).toLocaleTimeString("zh-CN", { hour12: false })),
      el("span", { class: "msg" }, event.message)));
  }
}

// ---------------------------------------------------------------------------
// datasets
// ---------------------------------------------------------------------------
async function loadDatasets(selectId) {
  const { ok, data } = await api("/api/datasets");
  if (!ok || !data) return;
  state.datasets = data.datasets || [];
  const select = $("#dataset-select");
  select.innerHTML = "";
  for (const dataset of state.datasets) {
    select.appendChild(el("option", { value: dataset.id },
      `${dataset.name}（${dataset.row_count} 行）`));
  }
  const target = selectId || state.datasetId || (state.datasets[0] && state.datasets[0].id);
  if (target) {
    select.value = target;
    await selectDataset(target);
  }
}

async function selectDataset(id) {
  state.datasetId = id;
  state.dirty = false;
  state.preview = null;
  state.selectedRow = 0;
  state.selectedField = null;
  $("#preview-box").hidden = true;
  showBanner("");
  const { ok, data } = await api(`/api/datasets/${id}`);
  if (!ok || !data) { toast("加载数据集失败", "bad"); return; }
  const draft = data.draft;
  const latest = await api(`/api/datasets/${id}/latest`);
  const version = latest.ok && latest.data ? latest.data.version : null;
  state.latest = version;
  if (draft) {
    state.fields = draft.fields || [];
    state.fx = draft.fx || {};
    state.output = draft.output || null;
  } else if (version) {
    state.fields = version.fields || [];
    state.fx = version.fx || {};
    state.output = version.outputs && version.outputs.selected;
  } else {
    state.fields = (data.dataset.columns || []).map((c) => ({
      name: c, kind: "raw", unit: "", label: c,
    }));
    state.fx = {};
  }
  renderFx();
  renderRawTable();
  renderDerivedList();
  if (version && version.outputs) {
    showData(version.outputs, version.version);
    $("#version-chip").textContent = `v${version.version} · 已发布`;
    $("#version-chip").classList.remove("stale");
  } else {
    showData(null, null);
    $("#version-chip").textContent = "未发布";
    $("#version-chip").classList.add("stale");
  }
  await loadVersions();
  await loadEvents();
  await runAnalysis();
}

// ---------------------------------------------------------------------------
// wiring
// ---------------------------------------------------------------------------
async function boot() {
  const { ok, data } = await api("/api/units");
  if (ok && data) {
    state.units = data.units || [];
    state.categories = data.categories || {};
  }
  $("#dataset-select").addEventListener("change", (e) => selectDataset(e.target.value));
  $("#btn-add-derived").addEventListener("click", addDerived);
  $("#btn-preview").addEventListener("click", previewChanges);
  $("#btn-publish").addEventListener("click", publish);
  $("#fx-usd-cny").addEventListener("input", () => {
    state.fx = readFx();
    markDirty();
    scheduleAnalysis();
  });
  $("#btn-import").addEventListener("click", () => { $("#import-modal").hidden = false; });
  $("#import-cancel").addEventListener("click", () => { $("#import-modal").hidden = true; });
  $("#import-submit").addEventListener("click", importCsv);
  await loadDatasets();
}

async function importCsv() {
  const name = $("#import-name").value.trim();
  const csv = $("#import-csv").value;
  if (!csv.trim()) { toast("请粘贴 CSV 内容", "bad"); return; }
  const { ok, data } = await api("/api/datasets", {
    method: "POST", body: { name, csv },
  });
  if (!ok || !data) {
    toast((data && data.error) || "导入失败", "bad");
    return;
  }
  $("#import-modal").hidden = true;
  $("#import-name").value = "";
  $("#import-csv").value = "";
  await loadDatasets(data.dataset.id);
  toast("导入成功，请为原始字段登记单位", "good");
}

boot();
