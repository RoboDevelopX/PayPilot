// PayPilot front end: chat, rules, approvals, invoices, payouts and the audit log.
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v, cur = "USD") => `${cur === "USD" ? "$" : cur + " "}${Number(v || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const ago = (ts) => {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
};

let sessionId = null;
try { sessionId = localStorage.getItem("paypilot-session"); } catch {}
if (!sessionId) {
  sessionId = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random())).replace(/-/g, "").slice(0, 24);
  try { localStorage.setItem("paypilot-session", sessionId); } catch {}
}

let state = null;

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

// ---------------------------------------------------------------- chat
const messages = $("#messages");

function welcome() {
  messages.innerHTML = `<div class="welcome"><h2>What should I take care of?</h2>
    <p>Tell me about a payment task in plain words. I'll create PayPal invoices, pay people, and chase late payers.
    Anything above your limit waits for your approval.</p></div>`;
}

function addBubble(text, who) {
  messages.querySelector(".welcome")?.remove();
  const el = document.createElement("div");
  el.className = `bubble ${who}`;
  el.textContent = text;
  messages.appendChild(el);
  messages.scrollTop = messages.scrollHeight;
  return el;
}

const TOOL_LABEL = {
  create_invoice: "Create invoice", send_payout: "Send payout", send_reminder: "Send reminder",
  cancel_invoice: "Cancel invoice", list_invoices: "Look up invoices", list_payouts: "Look up payouts",
  check_invoice: "Check invoice", get_rules: "Read rules",
};
const STATUS_LABEL = {
  executed: "Done in PayPal", pending_approval: "Needs your approval", denied: "Blocked by rules",
  invalid: "Rejected: invalid", paypal_error: "PayPal error",
};

function describe(ev) {
  const i = ev.input || {}, r = ev.result || {};
  if (ev.tool === "create_invoice") {
    const total = r.invoice?.total ?? (i.items || []).reduce((s, it) => s + Number(it.unit_amount || 0) * Number(it.quantity || 1), 0);
    return `${money(total, i.currency)} to ${i.recipient_email}`;
  }
  if (ev.tool === "send_payout") return `${money(i.amount, i.currency)} to ${i.recipient_email}`;
  if (ev.tool === "send_reminder") return `${i.invoice_id}${r.subject ? ` · “${r.subject}”` : ""}`;
  if (ev.tool === "cancel_invoice") return i.invoice_id;
  return "";
}

function addCard(ev) {
  const r = ev.result || {};
  if (!r.status) return; // read-only lookups don't need a card
  const el = document.createElement("div");
  el.className = `card ${r.status}`;
  const why = (r.reasons || []).join("; ") || r.error || "";
  el.innerHTML = `<div class="head">${esc(TOOL_LABEL[ev.tool] || ev.tool)} <span class="tag ${esc(r.status)}">${esc(STATUS_LABEL[r.status] || r.status)}</span></div>
    <div>${esc(describe(ev))}</div>${why ? `<div class="why">${esc(why)}</div>` : ""}`;
  messages.appendChild(el);
}

async function send(text) {
  text = text.trim();
  if (!text) return;
  addBubble(text, "user");
  $("#chat-input").value = "";
  $("#send-btn").disabled = true;
  const typing = addBubble("PayPilot is working…", "bot typing");
  try {
    const res = await api("/api/chat", { method: "POST", body: JSON.stringify({ session_id: sessionId, message: text }) });
    typing.remove();
    (res.events || []).forEach(addCard);
    addBubble(res.reply, "bot");
  } catch (e) {
    typing.remove();
    addBubble(`Something went wrong: ${e.message}`, "bot");
  } finally {
    $("#send-btn").disabled = false;
    messages.scrollTop = messages.scrollHeight;
    refresh();
  }
}

$("#chat-form").addEventListener("submit", (e) => { e.preventDefault(); send($("#chat-input").value); });
$("#chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(e.target.value); }
});
$("#chips").addEventListener("click", (e) => { const q = e.target.dataset?.q; if (q) send(q); });

// ---------------------------------------------------------------- rules
const form = $("#rules-form");
let rulesDirty = false;
form.addEventListener("input", () => { rulesDirty = true; });

function fillRules(r) {
  if (rulesDirty) return;
  for (const [k, v] of Object.entries(r)) {
    const el = form.elements[k];
    if (!el) continue;
    if (el.type === "checkbox") el.checked = !!v;
    else el.value = Array.isArray(v) ? v.join(", ") : v;
  }
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = form.elements, msg = $("#rules-msg");
  const body = {
    spending_limit: f.spending_limit.value, daily_limit: f.daily_limit.value, hard_cap: f.hard_cap.value,
    invoice_cap: f.invoice_cap.value,
    allowed_currencies: f.allowed_currencies.value.split(",").map((s) => s.trim()).filter(Boolean),
    approve_new_recipients: f.approve_new_recipients.checked, auto_reminders: f.auto_reminders.checked,
    reminder_after_hours: f.reminder_after_hours.value, reminder_every_hours: f.reminder_every_hours.value,
    max_reminders: f.max_reminders.value,
  };
  try {
    await api("/api/rules", { method: "PUT", body: JSON.stringify(body) });
    msg.className = "msg ok"; msg.textContent = "Saved. The agent follows these from now on.";
    rulesDirty = false;
    refresh();
  } catch (err) {
    msg.className = "msg err"; msg.textContent = err.message;
  }
});

// ---------------------------------------------------------------- side panels
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== `tab-${b.dataset.tab}`));
}));

function actionSummary(a) {
  if (a.type === "send_payout") return { title: `Pay ${money(a.amount, a.currency)}`, sub: `to ${a.recipient_email}${a.note ? ` · ${a.note}` : ""}` };
  if (a.type === "cancel_invoice") return { title: `Cancel invoice ${a.invoice_id}`, sub: `${money(a.amount, a.currency)} to ${a.recipient_email}` };
  return { title: a.type, sub: "" };
}

function renderApprovals(list) {
  const pending = list.filter((a) => a.status === "pending");
  const done = list.filter((a) => a.status !== "pending").slice(0, 8);
  const badge = $("#pending-badge");
  badge.textContent = pending.length;
  badge.classList.toggle("hidden", !pending.length);
  const card = (a, live) => {
    const s = actionSummary(a.action);
    return `<div class="item"><div class="top"><span class="amt">${esc(s.title)}</span><span class="tag ${esc(a.status)}">${esc(a.status)}</span></div>
      <div class="sub">${esc(s.sub)}</div>
      <ul>${a.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
      ${live ? `<div class="btns"><button class="approve" data-approve="${esc(a.id)}">Approve</button><button class="reject" data-reject="${esc(a.id)}">Reject</button></div>` : ""}</div>`;
  };
  $("#tab-approvals").innerHTML =
    (pending.length ? pending.map((a) => card(a, true)).join("") : `<div class="empty">Nothing waiting for you.</div>`) +
    (done.length ? `<h3>Decided</h3>${done.map((a) => card(a, false)).join("")}` : "");
}

function renderInvoices(list, mock) {
  $("#tab-invoices").innerHTML = list.length ? list.map((i) => `
    <div class="item"><div class="top"><span class="amt">${money(i.total, i.currency)}</span><span class="tag ${esc(i.status)}">${esc(i.status)}</span></div>
    <div class="sub">${esc(i.recipient_name || "")} ${esc(i.recipient_email)} · ${esc(i.id)}</div>
    <div class="sub">PayPal ${esc(i.paypal_id)} · reminders sent: ${i.reminders_sent}${i.sent_at ? ` · sent ${ago(i.sent_at)}` : ""}</div>
    <ul>${(i.items || []).map((it) => `<li>${esc(it.description)} × ${it.quantity} @ ${money(it.unit_amount, i.currency)}</li>`).join("")}</ul>
    <div class="btns">
      ${i.pay_url ? `<a href="${esc(i.pay_url)}" target="_blank" rel="noopener">Open PayPal payment page</a>` : ""}
      <button data-refresh="${esc(i.id)}">Refresh status</button>
      ${mock && ["SENT", "UNPAID"].includes(i.status) ? `<button data-paid="${esc(i.id)}">Simulate payment</button>` : ""}
    </div></div>`).join("") : `<div class="empty">No invoices yet. Try “Invoice a client”.</div>`;
}

function renderPayouts(list) {
  $("#tab-payouts").innerHTML = list.length ? list.map((p) => `
    <div class="item"><div class="top"><span class="amt">${money(p.amount, p.currency)}</span><span class="tag ${esc(p.status)}">${esc(p.status)}</span></div>
    <div class="sub">to ${esc(p.recipient_email)}${p.note ? ` · ${esc(p.note)}` : ""}</div>
    <div class="sub">${p.approved_by === "rules" ? "auto-approved by your rules" : "approved by you"} · ${ago(p.created_at)}${p.paypal_id ? ` · PayPal ${esc(p.paypal_id)}` : ""}</div></div>`).join("")
    : `<div class="empty">No payouts yet.</div>`;
}

const EVENT_TEXT = {
  proposed: (d) => `Agent proposed ${d.verdict.action.type.replace("_", " ")} → rules said <b>${esc(d.verdict.decision.replace("_", " "))}</b>`,
  executed: (d) => `Executed ${esc(d.type.replace("_", " "))}${d.paypal_id ? ` (PayPal ${esc(d.paypal_id)})` : ""}${d.by === "scheduler" ? " by the follow-up scheduler" : ""}`,
  queued_for_approval: (d) => `Queued for your approval: ${esc(d.reasons.join("; "))}`,
  approved_by_user: () => `You approved an action`,
  rejected_by_user: () => `You rejected an action`,
  rejected_invalid: (d) => `Rejected invalid ${esc(d.tool)}: ${esc(d.error)}`,
  denied: (d) => `Denied: ${esc((d.reasons || []).join("; "))}`,
  paypal_error: (d) => `PayPal error: ${esc(d.error)}`,
  rules_updated: () => `You updated your rules`,
  invoice_paid: (d) => `Invoice ${esc(d.invoice_id)} was paid 🎉`,
  webhook: (d) => `PayPal webhook: ${esc(d.event_type)}`,
};

function renderActivity(list) {
  $("#tab-activity").innerHTML = list.length ? list.map((e) => `<div class="log"><span class="when">${ago(e.created_at)}</span>${(EVENT_TEXT[e.event] || (() => esc(e.event)))(e.detail)}</div>`).join("")
    : `<div class="empty">Every proposal and decision is recorded here.</div>`;
}

function renderModes(m) {
  const pp = m.paypal === "sandbox" ? `<span class="pill live">PayPal sandbox · live</span>` : `<span class="pill mock">PayPal · mock mode</span>`;
  const ai = m.ai === "claude" ? `<span class="pill live">AI · ${esc(m.model)}</span>` : `<span class="pill mock">AI · offline parser</span>`;
  $("#modes").innerHTML = pp + ai;
  $("#reset").classList.toggle("hidden", m.paypal !== "mock");
}

async function refresh() {
  try {
    state = await api("/api/state");
  } catch { return; }
  renderModes(state.modes);
  fillRules(state.rules);
  const spent = Number(state.spent_today), daily = Number(state.rules.daily_limit) || 1;
  $("#spent-label").textContent = `${money(spent)} of ${money(daily)}`;
  $("#spent-bar").style.width = `${Math.min(100, (spent / daily) * 100)}%`;
  renderApprovals(state.approvals);
  renderInvoices(state.invoices, state.modes.paypal === "mock");
  renderPayouts(state.payouts);
  renderActivity(state.activity);
}

document.querySelector(".side").addEventListener("click", async (e) => {
  const t = e.target, d = t.dataset || {};
  try {
    if (d.approve || d.reject) {
      t.disabled = true;
      const id = d.approve || d.reject;
      const res = await api(`/api/approvals/${id}/${d.approve ? "approve" : "reject"}`, { method: "POST" });
      const what = res.status === "executed" ? "Approved and sent through PayPal." : res.status === "rejected" ? "Rejected. Nothing was sent." : `Result: ${res.status}${res.error ? ` (${res.error})` : ""}${res.reasons ? ` (${res.reasons.join("; ")})` : ""}`;
      addBubble(what, "bot");
    } else if (d.refresh) {
      await api(`/api/invoices/${d.refresh}/refresh`, { method: "POST" });
    } else if (d.paid) {
      await api(`/api/invoices/${d.paid}/simulate-paid`, { method: "POST" });
      addBubble(`Invoice ${d.paid} was paid. I'll stop following up on it.`, "bot");
    } else if (t.id === "run-reminders") {
      t.disabled = true;
      const res = await api("/api/reminders/run", { method: "POST" });
      const sent = res.results.filter((r) => r.status === "executed");
      addBubble(sent.length ? `Sent ${sent.length} follow-up${sent.length > 1 ? "s" : ""}:\n` + sent.map((r) => `• ${r.invoice_id}: “${r.subject}”\n  ${r.note}`).join("\n") : "No unpaid invoices need a follow-up right now.", "bot");
      t.disabled = false;
    } else if (t.id === "reset") {
      if (!confirm("Clear all demo data?")) return;
      await api("/api/reset", { method: "POST" });
      rulesDirty = false;
      welcome();
    } else return;
  } catch (err) {
    addBubble(`Something went wrong: ${err.message}`, "bot");
    t.disabled = false;
  }
  refresh();
});

welcome();
refresh();
setInterval(refresh, 5000);
