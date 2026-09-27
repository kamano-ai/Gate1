"use strict";
const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const inquiryId = params.get("id") || "1";
let state;
let busy = false;
function node(tag, text, cls) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}
function notice(message, success = false) {
  $("message").textContent = message;
  $("message").className = success ? "success" : "error";
  $("message").hidden = false;
  $("message").scrollIntoView({ behavior: "smooth", block: "nearest" });
}
function date(value) { return value.slice(0,16).replaceAll("-", "/"); }
function assigneeDisabled() {
  return state.inquiry.status === "DONE" || (state.actor.role === "MEMBER" && state.inquiry.assignee_id !== null);
}
function setBusy(value) {
  busy = value;
  $("status-form").querySelector("button").disabled = value;
  $("assignee-button").disabled = value || assigneeDisabled();
}
function render() {
  const q = state.inquiry;
  $("detail").hidden = false;
  $("actor").textContent = `${state.actor.name}（${state.actor.role === "ADMIN" ? "管理者" : "一般担当者"}）`;
  $("info").replaceChildren();
  for (const [label, text, cls] of [
    ["件名", q.title], ["依頼者", q.requester_name],
    ["優先度", {HIGH:"高",MIDDLE:"中",LOW:"低"}[q.priority], `priority-${q.priority}`],
    ["登録日", date(q.created_at)], ["本文", q.body]
  ]) $("info").append(node("dt",label), node("dd",text,cls));
  $("current-status").textContent = state.statuses[q.status];
  $("status-section").hidden = state.transitions.length === 0;
  $("status-options").replaceChildren();
  state.transitions.forEach((code, index) => {
    const label = node("label");
    const input = node("input");
    input.type = "radio"; input.name = "status"; input.value = code; input.checked = index === 0;
    label.append(input, document.createTextNode(state.statuses[code]));
    $("status-options").append(label);
  });
  $("current-assignee").textContent = q.assignee_name;
  $("assignee").replaceChildren();
  if (state.actor.role === "ADMIN") $("assignee").append(new Option("未割り当て", ""));
  state.users.forEach((u) => $("assignee").append(new Option(u.name, String(u.id))));
  $("assignee").value = q.assignee_id === null ? (state.actor.role === "ADMIN" ? "" : String(state.actor.id)) : String(q.assignee_id);
  $("assignee-notice").hidden = !(state.actor.role === "MEMBER" && q.assignee_id !== null);
  $("history").replaceChildren();
  if (state.history.length === 0) $("history").append(node("p", "変更履歴はありません", "hint"));
  for (const h of state.history) {
    const item = node("div", undefined, "history-item");
    item.append(node("div", `${date(h.changed_at)}　${h.changed_by_name}`, "history-meta"),
      node("div", `${h.field_name === "status" ? "ステータス" : "担当者"}： ${h.old_label} → ${h.new_label}`));
    $("history").append(item);
  }
  setBusy(busy);
}
async function load() {
  const response = await fetch(`/api/inquiries/${encodeURIComponent(inquiryId)}`);
  const data = await response.json();
  if (!response.ok) { $("detail").hidden = true; notice(data.message); return; }
  state = data;
  render();
}
async function change(operation, values) {
  if (busy || !state) return;
  setBusy(true);
  try {
    const response = await fetch(`/api/inquiries/${encodeURIComponent(inquiryId)}/${operation}`, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({...values, updated_at:state.inquiry.updated_at})
    });
    const result = await response.json();
    if (response.ok) {
      if (operation === "status") { $("comment").value = ""; $("comment-count").textContent = "0 / 200文字"; }
      await load();
    }
    notice(result.message, response.ok);
  } catch (error) { notice("通信に失敗しました。画面を再読み込みしてください。"); }
  finally { setBusy(false); }
}
$("comment").addEventListener("input", () => { $("comment-count").textContent = `${Array.from($("comment").value).length} / 200文字`; });
$("status-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const checked = document.querySelector('input[name="status"]:checked');
  if (checked) change("status", {status:checked.value, comment:$("comment").value});
});
$("assignee-form").addEventListener("submit", (event) => {
  event.preventDefault();
  change("assignee", {assignee_id:$("assignee").value === "" ? null : Number($("assignee").value)});
});
// Same UI request path, also callable from DevTools for the required invalid-transition capture.
window.gate1 = { changeStatus: (status) => change("status", {status, comment:$("comment").value}) };
async function start() {
  if (params.has("list")) {
    $("title").textContent = "問い合わせ一覧"; $("back").hidden = true;
    const response = await fetch("/api/inquiries");
    const rows = await response.json();
    if (!response.ok) { notice(rows.message); return; }
    $("list").hidden = false;
    for (const q of rows) {
      const link = node("a", q.title, "list-item"); link.href = `/?id=${q.id}`; $("list").append(link);
    }
  } else await load();
}
start().catch(() => notice("通信に失敗しました。画面を再読み込みしてください。"));
