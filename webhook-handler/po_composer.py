"""HTML + JS for the Purchase Order composer on the order-detail admin page.

Rendered into `get_order_html` (lpg-admin only). The composer drives the existing
PO endpoints with same-origin fetches — when the page is viewed through the admin
proxy (or local dev), those requests inherit the proxy's IAM auth, so no token
handling lives in the browser.

Flow: Generate PO -> editable line table (add/edit/delete, ADR-0022) -> inline PDF
preview -> Send to vendor (manual, with confirm). Regenerating from the order
discards manual edits, so it confirms once the PO has been hand-edited. Send is
never automatic; it honors the server's 409 double-send guard and surfaces
422 (no vendor email) / 502 (send failed) cleanly. A sent PO is read-only.

`__ORDER_ID__` is substituted at render time.
"""

PO_COMPOSER_TEMPLATE = r"""
<style>
  .po-actions { margin: 12px 0; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .btn { font: inherit; padding: 8px 16px; border: 1px solid #333; background: #fff;
         border-radius: 4px; cursor: pointer; }
  .btn:hover { background: #f5f5f5; }
  .btn-primary { background: #1a1a1a; color: #fff; border-color: #1a1a1a; }
  .btn-primary:hover { background: #333; }
  .btn-sm { padding: 4px 10px; font-size: 0.9em; }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .badge { display: inline-block; padding: 2px 10px; border-radius: 10px;
           font-size: 0.85em; font-weight: 500; }
  .badge-draft { background: #eee; color: #555; }
  .badge-sent { background: #d6f5d6; color: #1a7f1a; }
  .badge-edited { background: #fdf6e3; color: #8a6d1a; }
  .po-msg { margin: 10px 0; padding: 10px 14px; border-radius: 4px; font-size: 0.95em; }
  .po-msg.ok { background: #e8f6e8; color: #1a7f1a; }
  .po-msg.err { background: #fbe6e6; color: #b32020; }
  .po-msg.warn { background: #fdf6e3; color: #8a6d1a; }
  #po-lines input { font: inherit; padding: 4px 6px; border: 1px solid #ccc;
                    border-radius: 3px; width: 100%; box-sizing: border-box; }
  #po-lines td:nth-child(3), #po-lines th:nth-child(3),
  #po-lines td:nth-child(4), #po-lines th:nth-child(4),
  #po-lines td:nth-child(5), #po-lines th:nth-child(5),
  #po-lines td:nth-child(6), #po-lines th:nth-child(6) { text-align: right; }
  .po-preview { width: 100%; height: 540px; border: 1px solid #ddd;
                border-radius: 4px; margin-top: 12px; }
  /* --- PO modal --- */
  #po-modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.45);
                      display: none; align-items: flex-start; justify-content: center;
                      z-index: 1000; padding: 4vh 16px; overflow-y: auto; }
  #po-modal-overlay.open { display: flex; }
  #po-modal { background: #fff; border-radius: 8px; width: 100%; max-width: 820px;
              padding: 20px 24px 28px; box-shadow: 0 12px 40px rgba(0,0,0,0.25);
              position: relative; }
  #po-modal-close { position: absolute; top: 12px; right: 14px; border: none;
                    background: none; font-size: 1.6em; line-height: 1; cursor: pointer;
                    color: #888; }
  #po-modal-close:hover { color: #222; }
  .po-open-actions { margin: 8px 0 4px; }
</style>

<h2>Purchase Order</h2>
<div class="po-open-actions">
  <button class="btn btn-primary" onclick="openPOModal()">Open PO editor</button>
  <span id="po-open-hint" class="meta"></span>
</div>

<div id="po-modal-overlay" onclick="if(event.target===this)closePOModal()">
 <div id="po-modal" role="dialog" aria-modal="true" aria-label="Purchase Order editor">
  <button id="po-modal-close" onclick="closePOModal()" aria-label="Close">&times;</button>
  <h2 style="margin-top:0">Purchase Order</h2>
  <div id="po-section">
  <div class="po-actions">
    <button class="btn btn-primary" id="po-gen-btn" onclick="generatePO(false)">Generate PO</button>
    <span id="po-status" class="meta"></span>
  </div>
  <div id="po-msg-area"></div>
  <div id="po-detail" style="display:none">
    <table id="po-lines">
      <thead><tr>
        <th>Product ID</th><th>Description</th><th>Qty</th>
        <th>Unit Cost</th><th>Amount</th><th>Total</th><th></th>
      </tr></thead>
      <tbody></tbody>
      <tfoot></tfoot>
    </table>
    <div class="po-actions" id="po-add-actions">
      <button class="btn btn-sm" onclick="addRow(false)">+ Product line</button>
      <button class="btn btn-sm" onclick="addRow(true)">+ Fee line</button>
    </div>
    <div class="po-actions">
      <button class="btn" id="po-send-btn" onclick="sendPO()">Send to vendor</button>
    </div>
    <iframe class="po-preview" id="po-preview" title="PO PDF preview"></iframe>
  </div>
</div>
 </div>
</div>

<script>
const ORDER_ID = "__ORDER_ID__";
let currentPO = null;   // { po_number, status, manually_edited }

function openPOModal() {
  document.getElementById("po-modal-overlay").classList.add("open");
  document.body.style.overflow = "hidden";
}
function closePOModal() {
  document.getElementById("po-modal-overlay").classList.remove("open");
  document.body.style.overflow = "";
}
document.addEventListener("keydown", function (e) {
  if (e.key === "Escape") closePOModal();
});

function fmt(n) { return (n === null || n === undefined || n === "") ? "" : "$" + Number(n).toFixed(2); }

function msg(text, kind) {
  const el = document.getElementById("po-msg-area");
  el.innerHTML = text ? ('<div class="po-msg ' + kind + '">' + text + '</div>') : '';
}

function esc(s) { return (s === null || s === undefined) ? "" : String(s).replace(/"/g, "&quot;"); }

// Build one editable row. line === null => a new, unsaved row.
function rowHtml(line, isFee) {
  const sent = currentPO && currentPO.status === "sent";
  const id = line ? line.purchase_order_line_id : "new";
  const dis = sent ? "disabled" : "";
  const code = line ? esc(line.vendor_sku_code) : "";
  const desc = line ? esc(line.description) : "";
  const qty = line && line.quantity != null ? line.quantity : "";
  const cost = line && line.unit_cost != null ? line.unit_cost : "";
  const amt = line && line.amount != null ? line.amount : "";
  const total = line && line.line_total != null ? fmt(line.line_total) : "";
  const actions = sent ? "" :
    (line
      ? '<button class="btn btn-sm" onclick="saveLine(this)">Save</button> ' +
        '<button class="btn btn-sm" onclick="deleteLine(this)">×</button>'
      : '<button class="btn btn-sm btn-primary" onclick="saveLine(this)">Add</button> ' +
        '<button class="btn btn-sm" onclick="this.closest(\'tr\').remove()">×</button>');
  if (isFee) {
    return '<tr data-id="' + id + '" data-fee="true">' +
      '<td><span class="meta">(fee)</span></td>' +
      '<td><input class="l-desc" value="' + desc + '" placeholder="Fee label" ' + dis + '></td>' +
      '<td></td><td></td>' +
      '<td><input class="l-amt" type="number" step="0.01" value="' + amt + '" ' + dis + '></td>' +
      '<td>' + total + '</td><td>' + actions + '</td></tr>';
  }
  return '<tr data-id="' + id + '" data-fee="false">' +
    '<td><input class="l-code" value="' + code + '" placeholder="Product ID" ' + dis + '></td>' +
    '<td><input class="l-desc" value="' + desc + '" placeholder="Description" ' + dis + '></td>' +
    '<td><input class="l-qty" type="number" step="1" value="' + qty + '" ' + dis + '></td>' +
    '<td><input class="l-cost" type="number" step="0.01" value="' + cost + '" ' + dis + '></td>' +
    '<td></td>' +
    '<td>' + total + '</td><td>' + actions + '</td></tr>';
}

function renderLines(state) {
  currentPO = { po_number: state.po_number, status: state.status,
                manually_edited: state.manually_edited };
  document.getElementById("po-detail").style.display = "block";

  const sent = state.status === "sent";
  const badge = sent
    ? '<span class="badge badge-sent">sent</span>'
    : '<span class="badge badge-draft">draft</span>' +
      (state.manually_edited ? ' <span class="badge badge-edited">manually edited</span>' : '');
  document.getElementById("po-status").innerHTML = state.po_number + " " + badge;

  document.querySelector("#po-lines tbody").innerHTML =
    state.lines.map(function (l) { return rowHtml(l, l.is_fee); }).join("");
  document.querySelector("#po-lines tfoot").innerHTML =
    "<tr><td colspan='5' style='text-align:right;font-weight:600;border-top:2px solid #333'>Total</td>" +
    "<td style='font-weight:600;border-top:2px solid #333'>" + fmt(state.total) + "</td><td></td></tr>";

  document.getElementById("po-add-actions").style.display = sent ? "none" : "flex";
  const genBtn = document.getElementById("po-gen-btn");
  genBtn.textContent = state.manually_edited
    ? "Regenerate from order (discards edits)" : "Regenerate PO";
  const sendBtn = document.getElementById("po-send-btn");
  sendBtn.textContent = sent ? "Resend to vendor" : "Send to vendor";
}

function refreshPreview() {
  if (!currentPO) return;
  document.getElementById("po-preview").src =
    "/purchase-orders/" + encodeURIComponent(currentPO.po_number) + "/pdf?t=" + Date.now();
}

async function loadLines() {
  const r = await fetch("/purchase-orders/" + encodeURIComponent(currentPO.po_number) + "/lines");
  const data = await r.json().catch(function () { return {}; });
  if (!r.ok) { msg("Could not load lines: " + (data.error || ("HTTP " + r.status)), "err"); return; }
  renderLines(data);
  refreshPreview();
}

async function generatePO(force) {
  const btn = document.getElementById("po-gen-btn");
  btn.disabled = true; msg("", "");
  try {
    const url = "/orders/" + ORDER_ID + "/purchase-order" + (force ? "?force=true" : "");
    const r = await fetch(url, { method: "POST" });
    const data = await r.json().catch(function () { return {}; });
    if (r.status === 409) {
      if (confirm((data.error || "This discards manual edits.") + "\n\nRegenerate anyway?")) {
        btn.disabled = false; return generatePO(true);
      }
      return;
    }
    if (!r.ok) { msg("Generate failed: " + (data.error || ("HTTP " + r.status)), "err"); return; }
    currentPO = { po_number: data.po_number, status: "draft", manually_edited: false };
    if (data.unpriced_skus && data.unpriced_skus.length) {
      msg("Warning: unpriced SKUs were excluded — " + data.unpriced_skus.join(", "), "warn");
    }
    await loadLines();
  } catch (e) {
    msg("Generate error: " + e, "err");
  } finally {
    btn.disabled = false;
  }
}

function collectRow(tr) {
  const isFee = tr.getAttribute("data-fee") === "true";
  const get = function (cls) { const el = tr.querySelector(cls); return el ? el.value.trim() : ""; };
  if (isFee) {
    return { is_fee: true, description: get(".l-desc"), amount: get(".l-amt") };
  }
  return { is_fee: false, vendor_sku_code: get(".l-code"), description: get(".l-desc"),
           quantity: get(".l-qty"), unit_cost: get(".l-cost") };
}

async function lineFetch(url, method, body) {
  const r = await fetch(url, {
    method: method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined
  });
  const data = await r.json().catch(function () { return {}; });
  return { ok: r.ok, status: r.status, data: data };
}

async function saveLine(el) {
  const tr = el.closest("tr");
  const id = tr.getAttribute("data-id");
  const body = collectRow(tr);
  const isNew = id === "new";
  const base = "/purchase-orders/" + encodeURIComponent(currentPO.po_number) + "/lines";
  msg("", "");
  const res = await lineFetch(isNew ? base : base + "/" + id, isNew ? "POST" : "PATCH", body);
  if (!res.ok) { msg("Save failed: " + (res.data.error || ("HTTP " + res.status)), "err"); return; }
  renderLines(res.data); refreshPreview();
  msg(isNew ? "Line added ✓" : "Line saved ✓", "ok");
}

async function deleteLine(el) {
  const tr = el.closest("tr");
  const id = tr.getAttribute("data-id");
  if (!confirm("Delete this line?")) return;
  const url = "/purchase-orders/" + encodeURIComponent(currentPO.po_number) + "/lines/" + id;
  msg("", "");
  const res = await lineFetch(url, "DELETE", null);
  if (!res.ok) { msg("Delete failed: " + (res.data.error || ("HTTP " + res.status)), "err"); return; }
  renderLines(res.data); refreshPreview();
  msg("Line deleted ✓", "ok");
}

function addRow(isFee) {
  const tbody = document.querySelector("#po-lines tbody");
  tbody.insertAdjacentHTML("beforeend", rowHtml(null, isFee));
}

async function sendPO() {
  if (!currentPO) return;
  const isResend = currentPO.status === "sent";
  if (!confirm((isResend ? "Resend" : "Send") + " PO " + currentPO.po_number +
               " to the vendor? This emails the vendor.")) return;
  const btn = document.getElementById("po-send-btn");
  btn.disabled = true; btn.textContent = "Sending…"; msg("", "");
  try {
    const url = "/purchase-orders/" + encodeURIComponent(currentPO.po_number) +
                "/send" + (isResend ? "?force=true" : "");
    const r = await fetch(url, { method: "POST" });
    const data = await r.json().catch(function () { return {}; });
    if (r.status === 200) {
      msg("Sent to " + data.recipient + " ✓", "ok");
      await loadLines();
    } else if (r.status === 409) {
      msg((data.error || "Already sent") + " — use Resend to send again.", "warn");
      await loadLines();
    } else if (r.status === 422) {
      msg("Cannot send: " + (data.error || "vendor has no PO email set"), "err");
    } else {
      msg("Send failed: " + (data.error || ("HTTP " + r.status)), "err");
    }
  } catch (e) {
    msg("Send error: " + e, "err");
  } finally {
    btn.disabled = false;
  }
}
</script>
"""
