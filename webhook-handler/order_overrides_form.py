"""HTML + JS for the order-field override editor on the order-detail admin page.

Rendered into `get_order_html` (lpg-admin only). Like the PO composer, it drives
its endpoints with same-origin fetches — viewed through the admin proxy (or local
dev), requests inherit IAM auth, so no token handling lives in the browser.

The editor writes ONLY to lpg.order_overrides (ADR-0021); it never touches the
shift4.orders mirror. Each input shows the storefront (mirror) value as a
placeholder and holds the current override as its value. Blank = inherit from the
storefront. Clearing every field deletes the override row (full revert).

`__ORDER_ID__` is substituted at render time.
"""

ORDER_OVERRIDES_TEMPLATE = r"""
<style>
  #ov-section { margin-top: 1em; }
  #ov-section .ov-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  #ov-section fieldset { border: 1px solid #e0e0e0; border-radius: 4px; padding: 10px 14px; }
  #ov-section legend { font-weight: 500; color: #555; padding: 0 6px; }
  #ov-section label { display: block; font-size: 0.85em; color: #777; margin: 6px 0 2px; }
  #ov-section input, #ov-section textarea {
    width: 100%; box-sizing: border-box; font: inherit; padding: 5px 8px;
    border: 1px solid #ccc; border-radius: 3px; }
  #ov-section input.ov-set { border-color: #1a7f1a; background: #f6fcf6; }
  .ov-actions { margin: 12px 0; display: flex; gap: 8px; align-items: center; }
  #ov-msg-area .po-msg { margin: 10px 0; padding: 10px 14px; border-radius: 4px; font-size: 0.95em; }
  #ov-badge { display:inline-block; padding:2px 10px; border-radius:10px; font-size:0.85em; }
  #ov-badge.on { background:#d6f5d6; color:#1a7f1a; }
  #ov-badge.off { background:#eee; color:#777; }
</style>

<div id="ov-section">
  <h2>Order overrides <span id="ov-badge" class="off">none</span></h2>
  <p class="meta">LPG-owned corrections overlaying the storefront mirror (ADR-0021).
     Placeholder = storefront value. Leave blank to inherit; clear all to revert.</p>
  <div id="ov-msg-area"></div>
  <form id="ov-form" onsubmit="return false">
    <div class="ov-grid">
      <fieldset><legend>Ship-to</legend><div id="ov-ship"></div></fieldset>
      <fieldset><legend>Billing</legend><div id="ov-bill"></div></fieldset>
    </div>
    <label for="ov-comments">Comments</label>
    <textarea id="ov-comments" rows="2" data-key="comments"></textarea>
    <label for="ov-reason">Reason for override (recorded for audit)</label>
    <input id="ov-reason" type="text" placeholder="e.g. ship-to missing on order; confirmed with customer">
    <div class="ov-actions">
      <button class="btn btn-primary" id="ov-save-btn" onclick="saveOverrides()">Save overrides</button>
      <button class="btn" id="ov-clear-btn" onclick="clearOverrides()">Revert all to storefront</button>
      <span id="ov-status" class="meta"></span>
    </div>
  </form>
</div>

<script>
const OV_ORDER_ID = "__ORDER_ID__";
const OV_SHIP = [
  ["ship_to_first_name", "First name"], ["ship_to_last_name", "Last name"],
  ["ship_to_company", "Company"], ["ship_to_address", "Address"],
  ["ship_to_address2", "Address 2"], ["ship_to_city", "City"],
  ["ship_to_state", "State"], ["ship_to_zip", "ZIP"],
  ["ship_to_country", "Country"], ["ship_to_phone", "Phone"]
];
const OV_BILL = [
  ["bill_first_name", "First name"], ["bill_last_name", "Last name"],
  ["bill_company", "Company"], ["bill_address", "Address"],
  ["bill_address2", "Address 2"], ["bill_city", "City"],
  ["bill_state", "State"], ["bill_zip", "ZIP"],
  ["bill_country", "Country"], ["bill_phone", "Phone"], ["bill_email", "Email"]
];

function ovMsg(text, kind) {
  const el = document.getElementById("ov-msg-area");
  el.innerHTML = text ? ('<div class="po-msg ' + kind + '">' + text + '</div>') : '';
}

function ovInput(key, label) {
  return '<label for="ov-' + key + '">' + label + '</label>' +
         '<input id="ov-' + key + '" data-key="' + key + '" type="text">';
}

function ovMarkSet(el) {
  if (el.value && el.value.trim()) el.classList.add("ov-set");
  else el.classList.remove("ov-set");
}

function ovFill(data) {
  const mirror = data.mirror || {};
  const override = data.override || {};
  document.querySelectorAll("#ov-form [data-key]").forEach(function (el) {
    const k = el.getAttribute("data-key");
    el.value = (override[k] === null || override[k] === undefined) ? "" : override[k];
    el.placeholder = (mirror[k] === null || mirror[k] === undefined) ? "(blank)" : mirror[k];
    ovMarkSet(el);
    el.oninput = function () { ovMarkSet(el); };
  });
  document.getElementById("ov-reason").value = override.override_reason || "";
  const badge = document.getElementById("ov-badge");
  badge.textContent = data.has_override ? "active" : "none";
  badge.className = data.has_override ? "on" : "off";
}

function ovCollect() {
  const body = { override_reason: document.getElementById("ov-reason").value.trim() || null };
  document.querySelectorAll("#ov-form [data-key]").forEach(function (el) {
    const v = el.value.trim();
    body[el.getAttribute("data-key")] = v ? v : null;
  });
  return body;
}

async function ovLoad() {
  document.getElementById("ov-ship").innerHTML = OV_SHIP.map(function (f) { return ovInput(f[0], f[1]); }).join("");
  document.getElementById("ov-bill").innerHTML = OV_BILL.map(function (f) { return ovInput(f[0], f[1]); }).join("");
  try {
    const r = await fetch("/orders/" + OV_ORDER_ID + "/overrides");
    const data = await r.json().catch(function () { return {}; });
    if (!r.ok) { ovMsg("Could not load overrides: " + (data.error || ("HTTP " + r.status)), "err"); return; }
    ovFill(data);
  } catch (e) { ovMsg("Load error: " + e, "err"); }
}

async function ovPost(body, verb) {
  const btn = document.getElementById("ov-save-btn");
  const clr = document.getElementById("ov-clear-btn");
  btn.disabled = true; clr.disabled = true; ovMsg("", "");
  try {
    const r = await fetch("/orders/" + OV_ORDER_ID + "/overrides", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const data = await r.json().catch(function () { return {}; });
    if (!r.ok) { ovMsg(verb + " failed: " + (data.error || ("HTTP " + r.status)), "err"); return; }
    ovFill(data);
    ovMsg(data.has_override ? "Overrides saved ✓" : "Reverted to storefront ✓", "ok");
  } catch (e) {
    ovMsg(verb + " error: " + e, "err");
  } finally {
    btn.disabled = false; clr.disabled = false;
  }
}

function saveOverrides() { ovPost(ovCollect(), "Save"); }

function clearOverrides() {
  if (!confirm("Revert all fields on this order to the storefront values? The override row is deleted.")) return;
  const body = { override_reason: null };
  document.querySelectorAll("#ov-form [data-key]").forEach(function (el) {
    body[el.getAttribute("data-key")] = null;
  });
  ovPost(body, "Revert");
}

ovLoad();
</script>
"""
