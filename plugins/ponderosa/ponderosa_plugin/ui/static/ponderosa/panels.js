/**
 * Ponderosa Plugin UI Panels
 *
 * Provides custom panels for Build Orders, Sales Orders, and Parts
 * that display linked core-app data and sync status.
 */

const BASE_URL = '/plugin/ponderosa/';

async function fetchJSON(path) {
    const resp = await fetch(BASE_URL + path);
    return resp.json();
}

function formatDate(iso) {
    if (!iso) return '—';
    try {
        return new Date(iso).toLocaleString();
    } catch {
        return iso;
    }
}

function statusBadge(status) {
    const colors = {
        synced: '#2f9e44',
        pending: '#e8590c',
        error: '#e03131',
    };
    const color = colors[status] || '#868e96';
    return `<span style="
        display:inline-block;
        padding:2px 8px;
        border-radius:4px;
        font-size:12px;
        font-weight:600;
        color:#fff;
        background:${color};
    ">${status || 'unknown'}</span>`;
}

function card(title, rows) {
    let html = `<div style="border:1px solid #dee2e6;border-radius:8px;padding:16px;margin-bottom:12px;">`;
    if (title) {
        html += `<h4 style="margin:0 0 12px 0;font-size:15px;color:#495057;">${title}</h4>`;
    }
    for (const [label, value] of rows) {
        html += `<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid #f1f3f5;">
            <span style="color:#868e96;font-size:13px;">${label}</span>
            <span style="font-size:13px;font-weight:500;">${value ?? '—'}</span>
        </div>`;
    }
    html += `</div>`;
    return html;
}

function errorBox(message) {
    return `<div style="padding:12px;border-radius:8px;background:#fff5f5;border:1px solid #ffc9c9;color:#c92a2a;font-size:13px;">
        ${message}
    </div>`;
}

function infoBox(message) {
    return `<div style="padding:12px;border-radius:8px;background:#e7f5ff;border:1px solid #a5d8ff;color:#1971c2;font-size:13px;">
        ${message}
    </div>`;
}

function loading() {
    return `<div style="padding:20px;text-align:center;color:#868e96;">Loading...</div>`;
}

// ── Build Order → Job Detail Panel ──────────────────────────────────

export async function renderJobPanel(target, data) {
    if (!target) return;
    target.innerHTML = loading();

    try {
        const result = await fetchJSON(`api/job-detail/${data.id}/`);

        if (!result.linked) {
            target.innerHTML = infoBox('This Build Order is not linked to a Ponderosa job.');
            return;
        }

        if (result.error) {
            target.innerHTML = card('Ponderosa Job', [
                ['Core ID', `<code>${result.core_id}</code>`],
            ]) + errorBox(`Could not fetch job data: ${result.error}`);
            return;
        }

        const job = result.job || {};
        target.innerHTML = card('Job Info', [
            ['Job Number', `<strong>${job.jobNumber || '—'}</strong>`],
            ['Name', job.name || '—'],
            ['Status', job.status || '—'],
            ['Quantity', job.quantity ?? '—'],
            ['Due Date', formatDate(job.dueDate)],
        ]) + card('Sync', [
            ['Core App ID', `<code style="font-size:11px;">${result.core_id}</code>`],
        ]);
    } catch (err) {
        target.innerHTML = errorBox(`Failed to load job data: ${err.message}`);
    }
}

// ── Sales Order → Order Detail Panel ────────────────────────────────

export async function renderOrderPanel(target, data) {
    if (!target) return;
    target.innerHTML = loading();

    try {
        const result = await fetchJSON(`api/order-detail/${data.id}/`);

        if (!result.linked) {
            target.innerHTML = infoBox('This Sales Order is not linked to a Ponderosa order.');
            return;
        }

        if (result.error) {
            target.innerHTML = card('Ponderosa Order', [
                ['Core ID', `<code>${result.core_id}</code>`],
            ]) + errorBox(`Could not fetch order data: ${result.error}`);
            return;
        }

        const order = result.salesOrder || {};
        target.innerHTML = card('Order Info', [
            ['Order Number', `<strong>${order.orderNumber || '—'}</strong>`],
            ['Client', order.clientName || '—'],
            ['Status', order.status || '—'],
            ['Ship Date', formatDate(order.requestedShipDate)],
        ]) + card('Sync', [
            ['Core App ID', `<code style="font-size:11px;">${result.core_id}</code>`],
        ]);
    } catch (err) {
        target.innerHTML = errorBox(`Failed to load order data: ${err.message}`);
    }
}

// ── Part → Inventory Sync Panel ─────────────────────────────────────

export async function renderInventorySyncPanel(target, data) {
    if (!target) return;
    target.innerHTML = loading();

    try {
        const result = await fetchJSON(`api/inventory-sync/${data.id}/`);

        if (!result.linked) {
            target.innerHTML = infoBox('This Part is not linked to a Ponderosa inventory item.');
            return;
        }

        const delta = result.last_pushed_quantity != null
            ? result.inventree_quantity - result.last_pushed_quantity
            : null;
        const deltaStr = delta != null
            ? (delta === 0 ? '0 (in sync)' : `${delta > 0 ? '+' : ''}${delta}`)
            : '—';

        target.innerHTML = card('Stock Sync', [
            ['InvenTree Quantity', `<strong>${result.inventree_quantity}</strong>`],
            ['Last Pushed Quantity', result.last_pushed_quantity ?? '—'],
            ['Delta', deltaStr],
            ['Last Pushed', formatDate(result.last_pushed_at)],
        ]) + card('Sync Status', [
            ['Status', statusBadge(result.sync_status)],
            ['Last Synced', formatDate(result.last_synced_at)],
            ['Core App ID', `<code style="font-size:11px;">${result.core_id}</code>`],
        ]);
    } catch (err) {
        target.innerHTML = errorBox(`Failed to load sync data: ${err.message}`);
    }
}

// ── Visibility check ────────────────────────────────────────────────

export function isPanelHidden(context) {
    return false;
}
