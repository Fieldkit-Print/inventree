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
    if (!iso) return '\u2014';
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
            <span style="font-size:13px;font-weight:500;">${value ?? '\u2014'}</span>
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
            ['Job Number', `<strong>${job.jobNumber || '\u2014'}</strong>`],
            ['Name', job.name || '\u2014'],
            ['Status', job.status || '\u2014'],
            ['Quantity', job.quantity ?? '\u2014'],
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
            ['Order Number', `<strong>${order.orderNumber || '\u2014'}</strong>`],
            ['Client', order.clientName || '\u2014'],
            ['Status', order.status || '\u2014'],
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
            : '\u2014';

        target.innerHTML = card('Stock Sync', [
            ['InvenTree Quantity', `<strong>${result.inventree_quantity}</strong>`],
            ['Last Pushed Quantity', result.last_pushed_quantity ?? '\u2014'],
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

// ── Part → Production Routing Panel ─────────────────────────────────

export async function renderProductionRoutingPanel(target, data) {
    if (!target) return;
    target.innerHTML = loading();

    try {
        const templates = await fetchJSON(`api/parts/${data.id}/step-templates/`);

        if (!templates || templates.length === 0) {
            target.innerHTML = infoBox('No production step templates defined for this part. Add templates to define the production routing.');
            return;
        }

        let html = `<div style="margin-bottom:12px;">
            <h4 style="margin:0 0 8px 0;font-size:15px;color:#495057;">
                Production Routing (${templates.length} step${templates.length !== 1 ? 's' : ''})
            </h4>
        </div>`;

        html += `<div style="border:1px solid #dee2e6;border-radius:8px;overflow:hidden;">`;
        html += `<table style="width:100%;border-collapse:collapse;font-size:13px;">
            <thead>
                <tr style="background:#f8f9fa;border-bottom:2px solid #dee2e6;">
                    <th style="padding:8px 12px;text-align:left;color:#495057;">#</th>
                    <th style="padding:8px 12px;text-align:left;color:#495057;">Step</th>
                    <th style="padding:8px 12px;text-align:left;color:#495057;">Type</th>
                    <th style="padding:8px 12px;text-align:left;color:#495057;">Duration</th>
                </tr>
            </thead>
            <tbody>`;

        for (const tmpl of templates) {
            html += `<tr style="border-bottom:1px solid #f1f3f5;">
                <td style="padding:8px 12px;color:#868e96;">${tmpl.sequence}</td>
                <td style="padding:8px 12px;font-weight:500;">${tmpl.name}</td>
                <td style="padding:8px 12px;">${stepTypeBadge(tmpl.step_type)}</td>
                <td style="padding:8px 12px;color:#868e96;">${tmpl.estimated_duration || '\u2014'}</td>
            </tr>`;
        }

        html += `</tbody></table></div>`;
        target.innerHTML = html;
    } catch (err) {
        target.innerHTML = errorBox(`Failed to load production routing: ${err.message}`);
    }
}

// ── Build Order → Production Progress Panel ─────────────────────────

export async function renderProductionProgressPanel(target, data) {
    if (!target) return;
    target.innerHTML = loading();

    try {
        const result = await fetchJSON(`api/builds/${data.id}/steps/`);

        if (!result.steps || result.steps.length === 0) {
            target.innerHTML = infoBox('No production steps for this build order.');
            return;
        }

        const p = result.progress;

        // Progress bar
        let html = `<div style="margin-bottom:16px;">
            <div style="display:flex;justify-content:space-between;margin-bottom:4px;">
                <span style="font-size:13px;color:#495057;font-weight:500;">Progress</span>
                <span style="font-size:13px;color:#495057;">${p.completed}/${p.total} steps (${p.percent_complete}%)</span>
            </div>
            <div style="height:8px;background:#e9ecef;border-radius:4px;overflow:hidden;">
                <div style="height:100%;width:${p.percent_complete}%;background:#2f9e44;border-radius:4px;transition:width 0.3s;"></div>
            </div>
            <div style="display:flex;gap:12px;margin-top:8px;font-size:11px;color:#868e96;">
                <span>${stepStatusDot('#868e96')} ${p.pending} pending</span>
                ${p.queued ? `<span>${stepStatusDot('#fab005')} ${p.queued} queued</span>` : ''}
                <span>${stepStatusDot('#1971c2')} ${p.in_progress} in progress</span>
                <span>${stepStatusDot('#2f9e44')} ${p.completed} completed</span>
                ${p.on_hold ? `<span>${stepStatusDot('#e8590c')} ${p.on_hold} on hold</span>` : ''}
                ${p.blocked ? `<span>${stepStatusDot('#e03131')} ${p.blocked} blocked</span>` : ''}
                ${p.skipped ? `<span>${stepStatusDot('#adb5bd')} ${p.skipped} skipped</span>` : ''}
            </div>
        </div>`;

        // Steps list
        html += `<div style="border:1px solid #dee2e6;border-radius:8px;overflow:hidden;">`;
        html += `<table style="width:100%;border-collapse:collapse;font-size:13px;">
            <thead>
                <tr style="background:#f8f9fa;border-bottom:2px solid #dee2e6;">
                    <th style="padding:8px 12px;text-align:left;color:#495057;">#</th>
                    <th style="padding:8px 12px;text-align:left;color:#495057;">Step</th>
                    <th style="padding:8px 12px;text-align:left;color:#495057;">Type</th>
                    <th style="padding:8px 12px;text-align:left;color:#495057;">Station</th>
                    <th style="padding:8px 12px;text-align:left;color:#495057;">Status</th>
                    <th style="padding:8px 12px;text-align:left;color:#495057;">Time</th>
                </tr>
            </thead>
            <tbody>`;

        for (const step of result.steps) {
            const timeStr = step.completed_at
                ? formatDate(step.completed_at)
                : step.started_at
                    ? `Started ${formatDate(step.started_at)}`
                    : '\u2014';
            html += `<tr style="border-bottom:1px solid #f1f3f5;">
                <td style="padding:8px 12px;color:#868e96;">${step.sequence}</td>
                <td style="padding:8px 12px;font-weight:500;">${step.name}</td>
                <td style="padding:8px 12px;">${stepTypeBadge(step.step_type)}</td>
                <td style="padding:8px 12px;">${step.station ? step.station.name : '<span style="color:#adb5bd;">unassigned</span>'}</td>
                <td style="padding:8px 12px;">${stepStatusBadge(step.status)}</td>
                <td style="padding:8px 12px;font-size:11px;color:#868e96;">${timeStr}</td>
            </tr>`;
        }

        html += `</tbody></table></div>`;
        target.innerHTML = html;
    } catch (err) {
        target.innerHTML = errorBox(`Failed to load production steps: ${err.message}`);
    }
}

// ── Helper: step status badges ──────────────────────────────────────

function stepStatusBadge(status) {
    const colors = {
        pending: '#868e96',
        queued: '#fab005',
        in_progress: '#1971c2',
        completed: '#2f9e44',
        on_hold: '#e8590c',
        blocked: '#e03131',
        skipped: '#adb5bd',
    };
    const color = colors[status] || '#868e96';
    const textColor = status === 'queued' ? '#212529' : '#fff';
    const label = (status || 'unknown').replace('_', ' ');
    return `<span style="
        display:inline-block;
        padding:2px 8px;
        border-radius:4px;
        font-size:11px;
        font-weight:600;
        color:${textColor};
        background:${color};
    ">${label}</span>`;
}

function stepStatusDot(color) {
    return `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${color};vertical-align:middle;"></span>`;
}

function stepTypeBadge(stepType) {
    if (!stepType) return '\u2014';
    const color = stepType.color || '#1971c2';
    return `<span style="
        display:inline-block;
        padding:2px 6px;
        border-radius:3px;
        font-size:11px;
        background:${color}20;
        color:${color};
        text-transform:capitalize;
    ">${stepType.name || stepType.slug || ''}</span>`;
}

// ── Visibility check ────────────────────────────────────────────────

export function isPanelHidden(context) {
    return false;
}
