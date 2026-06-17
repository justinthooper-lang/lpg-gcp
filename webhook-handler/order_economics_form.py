"""HTML + JS for the order-detail "Economics" section (margin + manual entry).

Rendered into `get_order_html` (lpg-admin only). Like the overrides editor and
PO composer, it drives /orders/{id}/margin with same-origin fetches — viewed
through the admin proxy (or local dev), requests inherit IAM auth, so no token
handling lives in the browser.

Shows the effective margin from lpg.v_order_margins: supplier cost, actual
freight, profit, and shipping differential, with a badge for margin_source
('invoice' | 'manual' | 'none'). When the order is matched to a Crown invoice the
figures are read-only ('from Crown invoice'). When there's no invoice, the cost
and freight inputs are editable and save to lpg.order_margin_manual (ADR-0025);
a real invoice arriving later always supersedes the manual entry.

`__ORDER_ID__` is substituted at render time.
"""

ORDER_ECONOMICS_TEMPLATE = r"""
<style>
  #ec-section { margin-top: 2em; }
  #ec-section .ec-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px 24px; max-width: 640px; }
  #ec-section .ec-row { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #f0f0f0; }
  #ec-section .ec-row .lbl { color: #777; }
  #ec-section .ec-row .val { font-variant-numeric: tabular-nums; font-weight: 500; }
  #ec-section .val.neg { color: #c0392b; }
  #ec-section .val.pos { color: #1a7f1a; }
  #ec-edit { margin-top: 14px; max-width: 640px; }
  #ec-edit label { display: block; font-size: 0.85em; color: #777; margin: 8px 0 2px; }
  #ec-edit input { width: 100%; box-sizing: border-box; font: inherit; padding: 6px 8px;
                   border: 1px solid #ccc; border-radius: 3px; }
  #ec-edit input:disabled { background: #f5f5f5; color: #999; }
  .ec-actions { margin: 12px 0; display: flex; gap: 8px; align-items: center; }
  #ec-msg-area .po-msg { margin: 10px 0; padding: 10px 14px; border-radius: 4px; font-size: 0.95em; }
  #ec-msg-area .po-msg.ok { background: #e8f6e8; color: #1a7f1a; }
  #ec-msg-area .po-msg.err { background: #fbe6e6; color: #b32020; }
  #ec-msg-area .po-msg.warn { background: #fdf6e3; color: #8a6d1a; }
  #ec-badge { display: inline-block; padding: 2px 10px; border-radius: 10px; font-size: 0.85em; font-weight: 500; }
  #ec-badge.invoice { background: #d6f5d6; color: #1a7f1a; }
  #ec-badge.manual  { background: #fdf6e3; color: #8a6d1a; }
  #ec-badge.none    { background: #eee; color: #777; }
</style>

<div id="ec-section">
  <h2>Economics <span id="ec-badge" class="none">—</span></h2>
  <p class="meta" id="ec-note"></p>
  <div id="ec-msg-area"></div>

  <div class="ec-grid" id="ec-figures"></div>

  <div id="ec-edit" style="display:none">
    <label>Supplier cost (Crown product cost)</label>
    <input id="ec-cost" type="number" step="0.01" min="0" placeholder="0.00">
    <label>Actual freight (truck or UPS)</label>
    <input id="ec-freight" type="number" step="0.01" min="0" placeholder="0.00">
    <div class="ec-actions">
      <button class="btn btn-primary" id="ec-save-btn" onclick="ecSave()">Save manual margin</button>
      <button class="btn" id="ec-clear-btn" onclick="ecClear()">Clear</button>
    </div>
  </div>
</div>

<script>
const EC_ORDER_ID = "__ORDER_ID__";

function ecMsg(text, kind) {
  const el = document.getElementById("ec-msg-area");
  el.innerHTML = text ? ('<div class="po-msg ' + (kind || "") + '">' + text + '</div>') : '';
}
function ecFmt(v) {
  return (v === null || v === undefined) ? '—' : '$' + Number(v).toFixed(2);
}
function ecRow(label, v, cls) {
  return '<div class="ec-row"><span class="lbl">' + label + '</span>' +
         '<span class="val ' + (cls || '') + '">' + ecFmt(v) + '</span></div>';
}

function ecRender(d) {
  const badge = document.getElementById("ec-badge");
  badge.className = d.margin_source;
  badge.textContent = d.margin_source === 'invoice' ? 'from Crown invoice'
                    : d.margin_source === 'manual'  ? 'manual entry'
                    : 'no cost yet';
  document.getElementById("ec-note").textContent =
      d.margin_source === 'invoice'
        ? 'Cost and freight come from the matched Crown invoice and cannot be edited here.'
      : d.margin_source === 'manual'
        ? 'Manually entered. A matched Crown invoice will automatically supersede this.'
        : 'No matched Crown invoice yet. Enter the supplier cost and freight to compute margin.';

  const profitCls = d.profit === null ? '' : (d.profit < 0 ? 'neg' : 'pos');
  const diffCls = d.shipping_differential === null ? '' : (d.shipping_differential < 0 ? 'neg' : 'pos');
  document.getElementById("ec-figures").innerHTML =
      ecRow('Revenue (grand total)', d.grand_total) +
      ecRow('Shipping charged', d.shipping_cost) +
      ecRow('Supplier cost', d.supplier_cost) +
      ecRow('Actual freight', d.actual_freight) +
      ecRow('Profit', d.profit, profitCls) +
      ecRow('Shipping differential', d.shipping_differential, diffCls);

  // Editable only when not invoice-matched.
  const edit = document.getElementById("ec-edit");
  if (d.editable) {
    edit.style.display = '';
    document.getElementById("ec-cost").value =
        d.manual_supplier_cost !== null ? d.manual_supplier_cost : '';
    document.getElementById("ec-freight").value =
        d.manual_freight !== null ? d.manual_freight : '';
  } else {
    edit.style.display = 'none';
  }
}

async function ecLoad() {
  try {
    const r = await fetch("/orders/" + EC_ORDER_ID + "/margin");
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      ecMsg("Could not load margin: " + (e.error || ("HTTP " + r.status)), "err");
      return;
    }
    ecRender(await r.json());
  } catch (e) {
    ecMsg("Could not load margin: " + e, "err");
  }
}

async function ecPost(body) {
  ecMsg("", "");
  try {
    const r = await fetch("/orders/" + EC_ORDER_ID + "/margin", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      ecMsg("Save failed: " + (d.error || ("HTTP " + r.status)), "err");
      return;
    }
    ecRender(d);
    ecMsg("Saved.", "ok");
  } catch (e) {
    ecMsg("Save error: " + e, "err");
  }
}

function ecSave() {
  const cost = document.getElementById("ec-cost").value.trim();
  const freight = document.getElementById("ec-freight").value.trim();
  if (cost === '' || freight === '') {
    ecMsg("Enter both supplier cost and freight.", "warn");
    return;
  }
  ecPost({ manual_supplier_cost: cost, manual_freight: freight });
}

function ecClear() {
  if (!confirm("Clear the manual margin entry for this order?")) return;
  ecPost({ manual_supplier_cost: null, manual_freight: null });
}

ecLoad();
</script>
"""
