"""HTML + JS for the Purchase Order composer on the order-detail admin page.

Rendered into `get_order_html` (lpg-admin only). The composer drives the existing
PO endpoints with same-origin fetches — when the page is viewed through the admin
proxy (or local dev), those requests inherit the proxy's IAM auth, so no token
handling lives in the browser.

Flow: Generate PO -> inline PDF preview -> Send to vendor (manual, with confirm).
Send is never automatic; it honors the server's 409 double-send guard and surfaces
422 (no vendor email) / 502 (send failed) cleanly.

`__ORDER_ID__` is substituted at render time.
"""

PO_COMPOSER_TEMPLATE = r"""
<style>
  .po-actions { margin: 12px 0; display: flex; gap: 8px; align-items: center; }
  .btn { font: inherit; padding: 8px 16px; border: 1px solid #333; background: #fff;
         border-radius: 4px; cursor: pointer; }
  .btn:hover { background: #f5f5f5; }
  .btn-primary { background: #1a1a1a; color: #fff; border-color: #1a1a1a; }
  .btn-primary:hover { background: #333; }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .badge { display: inline-block; padding: 2px 10px; border-radius: 10px;
           font-size: 0.85em; font-weight: 500; }
  .badge-draft { background: #eee; color: #555; }
  .badge-sent { background: #d6f5d6; color: #1a7f1a; }
  .po-msg { margin: 10px 0; padding: 10px 14px; border-radius: 4px; font-size: 0.95em; }
  .po-msg.ok { background: #e8f6e8; color: #1a7f1a; }
  .po-msg.err { background: #fbe6e6; color: #b32020; }
  .po-msg.warn { background: #fdf6e3; color: #8a6d1a; }
  #po-lines td:last-child, #po-lines th:last-child,
  #po-lines td:nth-child(3), #po-lines th:nth-child(3),
  #po-lines td:nth-child(4), #po-lines th:nth-child(4) { text-align: right; }
  .po-preview { width: 100%; height: 540px; border: 1px solid #ddd;
                border-radius: 4px; margin-top: 12px; }
</style>

<h2>Purchase Order</h2>
<div id="po-section">
  <div class="po-actions">
    <button class="btn btn-primary" id="po-gen-btn" onclick="generatePO()">Generate PO</button>
    <span id="po-status" class="meta"></span>
  </div>
  <div id="po-msg-area"></div>
  <div id="po-detail" style="display:none">
    <table id="po-lines">
      <thead><tr>
        <th>Product ID</th><th>Description</th><th>Qty</th><th>Unit Cost</th><th>Amount</th>
      </tr></thead>
      <tbody></tbody>
    </table>
    <div class="po-actions">
      <button class="btn" id="po-send-btn" onclick="sendPO()">Send to vendor</button>
    </div>
    <iframe class="po-preview" id="po-preview" title="PO PDF preview"></iframe>
  </div>
</div>

<script>
const ORDER_ID = "__ORDER_ID__";
let currentPO = null;

function fmt(n) { return (n === null || n === undefined) ? "" : "$" + Number(n).toFixed(2); }

function msg(text, kind) {
  const el = document.getElementById("po-msg-area");
  el.innerHTML = text ? ('<div class="po-msg ' + kind + '">' + text + '</div>') : '';
}

function renderPO(data) {
  document.getElementById("po-detail").style.display = "block";
  const badge = data.status === "sent"
    ? '<span class="badge badge-sent">sent</span>'
    : '<span class="badge badge-draft">draft</span>';
  document.getElementById("po-status").innerHTML =
    data.po_number + " " + badge + (data.regenerated ? " (regenerated)" : "");

  let total = 0;
  const rows = data.lines.map(function (l) {
    const amount = l.is_fee ? Number(l.amount) : Number(l.quantity) * Number(l.unit_cost);
    total += amount;
    return "<tr><td>" + (l.vendor_sku_code || "") + "</td><td>" + (l.description || "") +
      "</td><td>" + (l.is_fee ? "" : l.quantity) +
      "</td><td>" + (l.is_fee ? "" : fmt(l.unit_cost)) +
      "</td><td>" + fmt(amount) + "</td></tr>";
  }).join("");
  const totalRow =
    "<tr><td colspan='4' style='text-align:right;font-weight:600;border-top:2px solid #333'>Total</td>" +
    "<td style='font-weight:600;border-top:2px solid #333'>" + fmt(total) + "</td></tr>";
  document.querySelector("#po-lines tbody").innerHTML = rows + totalRow;

  const sendBtn = document.getElementById("po-send-btn");
  sendBtn.textContent = data.status === "sent" ? "Resend to vendor" : "Send to vendor";
}

async function generatePO() {
  const btn = document.getElementById("po-gen-btn");
  btn.disabled = true; btn.textContent = "Generating\u2026"; msg("", "");
  try {
    const r = await fetch("/orders/" + ORDER_ID + "/purchase-order", { method: "POST" });
    const data = await r.json().catch(function () { return {}; });
    if (!r.ok) { msg("Generate failed: " + (data.error || ("HTTP " + r.status)), "err"); return; }
    currentPO = data;
    renderPO(data);
    document.getElementById("po-preview").src =
      "/purchase-orders/" + encodeURIComponent(data.po_number) + "/pdf?t=" + Date.now();
    if (data.unpriced_skus && data.unpriced_skus.length) {
      msg("Warning: unpriced SKUs were excluded \u2014 " + data.unpriced_skus.join(", "), "warn");
    }
  } catch (e) {
    msg("Generate error: " + e, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = currentPO ? "Regenerate PO" : "Generate PO";
  }
}

async function sendPO() {
  if (!currentPO) return;
  const isResend = currentPO.status === "sent";
  if (!confirm((isResend ? "Resend" : "Send") + " PO " + currentPO.po_number +
               " to the vendor? This emails the vendor.")) return;
  const btn = document.getElementById("po-send-btn");
  btn.disabled = true; btn.textContent = "Sending\u2026"; msg("", "");
  try {
    const url = "/purchase-orders/" + encodeURIComponent(currentPO.po_number) +
                "/send" + (isResend ? "?force=true" : "");
    const r = await fetch(url, { method: "POST" });
    const data = await r.json().catch(function () { return {}; });
    if (r.status === 200) {
      currentPO.status = "sent";
      renderPO(currentPO);
      msg("Sent to " + data.recipient + " \u2713", "ok");
    } else if (r.status === 409) {
      currentPO.status = "sent";
      renderPO(currentPO);
      msg((data.error || "Already sent") + " \u2014 use Resend to send again.", "warn");
    } else if (r.status === 422) {
      msg("Cannot send: " + (data.error || "vendor has no PO email set"), "err");
    } else {
      msg("Send failed: " + (data.error || ("HTTP " + r.status)), "err");
    }
  } catch (e) {
    msg("Send error: " + e, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = currentPO.status === "sent" ? "Resend to vendor" : "Send to vendor";
  }
}
</script>
"""
