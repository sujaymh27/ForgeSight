const MACHINE_IDS = ['CNC_01', 'CNC_02', 'PUMP_03', 'CONVEYOR_04'];
const MACHINE_NAMES = { 'CNC_01': 'CNC Machine #1', 'CNC_02': 'CNC Machine #2', 'PUMP_03': 'Pump #3', 'CONVEYOR_04': 'Conveyor #4' };
const SENSOR_RANGES = {
    temperature_C: { min: 0, max: 120, unit: '°C', label: 'Temp' },
    vibration_mm_s: { min: 0, max: 18, unit: 'mm/s', label: 'Vibration' },
    rpm: { min: 0, max: 3500, unit: 'RPM', label: 'RPM' },
    current_A: { min: 0, max: 30, unit: 'A', label: 'Current' },
};
const SENSOR_FIELDS = ['temperature_C', 'vibration_mm_s', 'rpm', 'current_A'];

const machineState = {};
MACHINE_IDS.forEach(mid => {
    machineState[mid] = { riskScore: 0, status: 'running', readings: {}, baselines: {}, anomalies: {}, dataGap: false, anomalyType: 'none', suppressed: 0 };
});

let alerts = [];
let priorityQueue = [];
let maintenanceSlots = [];
let prevPriorities = {};

function initMachineCards() {
    const grid = document.getElementById('machines-grid');
    MACHINE_IDS.forEach(mid => {
        const card = document.createElement('div');
        card.id = `card-${mid}`;
        card.className = 'machine-card risk-low';
        card.innerHTML = `
            <div class="card-header">
                <div>
                    <h3>${mid}</h3>
                    <div class="machine-name">${MACHINE_NAMES[mid]}</div>
                </div>
                <span id="status-${mid}" class="badge badge-running">RUNNING</span>
            </div>
            <div class="risk-gauge-row">
                <div class="risk-gauge" id="gauge-${mid}">
                    <div class="risk-gauge-bg"></div>
                    <span class="risk-gauge-value" id="gauge-val-${mid}">0</span>
                </div>
                <div class="risk-label">
                    <strong>Risk Score</strong>
                    <span id="risk-class-${mid}">Normal</span>
                </div>
            </div>
            <div id="atype-${mid}" class="anomaly-type-badge at-none"></div>
            <div class="sensor-rows" id="sensors-${mid}">
                ${SENSOR_FIELDS.map(f => `
                    <div class="sensor-row">
                        <span class="sensor-label">${SENSOR_RANGES[f].label}</span>
                        <span class="sensor-value" id="val-${mid}-${f}">--</span>
                        <div class="sensor-bar-track" id="bar-${mid}-${f}">
                            <div class="sensor-bar-safe"></div>
                            <div class="sensor-bar-dot"></div>
                        </div>
                    </div>
                `).join('')}
            </div>
            <div class="data-gap-overlay" id="gap-${mid}" style="display:none;">DATA GAP — NO SIGNAL</div>
        `;
        grid.appendChild(card);
    });
}

function riskClass(score) { return score > 75 ? 'critical' : score > 50 ? 'high' : score > 25 ? 'medium' : score > 10 ? 'low' : 'normal'; }
function riskLabel(cls) { return { normal: 'Normal', low: 'Low', medium: 'Moderate', high: 'High', critical: 'Critical' }[cls] || 'Normal'; }
function riskColor(cls) { return { normal: 'var(--green)', low: 'var(--green)', medium: 'var(--yellow)', high: 'var(--orange)', critical: 'var(--red)' }[cls] || 'var(--green)'; }

function updateMachineCard(mid) {
    const st = machineState[mid];
    const cls = riskClass(st.riskScore);
    const card = document.getElementById(`card-${mid}`);
    card.className = `machine-card risk-${cls}` + (st.dataGap ? ' data-gap' : '');

    const statusEl = document.getElementById(`status-${mid}`);
    statusEl.className = `badge badge-${st.status}`;
    statusEl.textContent = st.status.toUpperCase();

    const gauge = document.getElementById(`gauge-${mid}`);
    const pct = Math.min(st.riskScore, 100);
    gauge.style.setProperty('--gauge-pct', pct);
    gauge.style.setProperty('--gauge-color', riskColor(cls));
    document.getElementById(`gauge-val-${mid}`).textContent = Math.round(pct);
    document.getElementById(`gauge-val-${mid}`).style.color = riskColor(cls);
    document.getElementById(`risk-class-${mid}`).textContent = riskLabel(cls);
    document.getElementById(`risk-class-${mid}`).style.color = riskColor(cls);

    // Anomaly Type Badge
    const atypeEl = document.getElementById(`atype-${mid}`);
    const typeMap = { spike: 'Spike Detected', drift: 'Gradual Drift', compound: 'Compound Anomaly', none: 'None' };
    const classMap = { spike: 'at-spike', drift: 'at-drift', compound: 'at-compound', none: 'at-none' };
    atypeEl.textContent = typeMap[st.anomalyType] || 'None';
    atypeEl.className = `anomaly-type-badge ${classMap[st.anomalyType] || 'at-none'}`;

    SENSOR_FIELDS.forEach(f => {
        const range = SENSOR_RANGES[f];
        const val = st.readings[f];
        const bl = st.baselines[f];
        const isAnomalous = !!st.anomalies[f];

        const valEl = document.getElementById(`val-${mid}-${f}`);
        valEl.textContent = val != null ? `${val.toFixed(range.label === 'RPM' ? 0 : 1)}${range.unit}` : '--';
        valEl.className = 'sensor-value' + (isAnomalous ? ' anomalous' : '');

        const bar = document.getElementById(`bar-${mid}-${f}`);
        const safeBar = bar.querySelector('.sensor-bar-safe');
        const dot = bar.querySelector('.sensor-bar-dot');

        if (bl && val != null) {
            const totalRange = range.max - range.min;
            const safeLeft = Math.max(0, ((bl.lower - range.min) / totalRange) * 100);
            const safeWidth = Math.max(0, ((bl.upper - bl.lower) / totalRange) * 100);
            const valPos = Math.max(0, Math.min(100, ((val - range.min) / totalRange) * 100));
            safeBar.style.left = safeLeft + '%';
            safeBar.style.width = safeWidth + '%';
            dot.style.left = valPos + '%';
            dot.className = 'sensor-bar-dot' + (isAnomalous ? ' anomalous' : '');
        }
    });
    document.getElementById(`gap-${mid}`).style.display = st.dataGap ? 'flex' : 'none';
}

function updatePriorityQueue() {
    const container = document.getElementById('priority-queue');
    document.getElementById('pq-count').textContent = priorityQueue.length;
    if (!priorityQueue.length) { container.innerHTML = '<p class="empty-state">No escalations</p>'; return; }
    
    container.innerHTML = priorityQueue.map((item, i) => {
        const rankCls = i === 0 ? 'r1' : i === 1 ? 'r2' : i === 2 ? 'r3' : 'rn';
        const scoreColor = riskColor(riskClass(item.risk_score));
        const prevPrio = prevPriorities[item.machine_id];
        const isEscalated = prevPrio && prevPrio !== item.priority && 
                            ['info','low','medium','high','critical'].indexOf(item.priority) > ['info','low','medium','high','critical'].indexOf(prevPrio);
        
        return `
            <div class="pq-item ${isEscalated ? 'escalated' : ''}">
                <span class="pq-rank ${rankCls}">${i + 1}</span>
                <div class="pq-info">
                    <div class="pq-mid">${item.machine_id} ${isEscalated ? '<span class="badge badge-critical" style="font-size:8px">ESCALATED</span>' : ''}</div>
                    <div class="pq-reason">${item.reason}</div>
                </div>
                <span class="pq-score" style="color:${scoreColor}">${Math.round(item.risk_score)}</span>
            </div>
        `;
    }).join('');

    priorityQueue.forEach(item => prevPriorities[item.machine_id] = item.priority);
}

function updateMaintenanceSchedule() {
    const container = document.getElementById('maintenance-schedule');
    document.getElementById('maint-count').textContent = maintenanceSlots.length;
    if (!maintenanceSlots.length) { container.innerHTML = '<p class="empty-state">No scheduled slots</p>'; return; }
    container.innerHTML = maintenanceSlots.map(slot => {
        const time = new Date(slot.scheduled_time);
        const timeStr = time.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        return `
            <div class="maint-item">
                <span class="maint-icon">&#128295;</span>
                <div class="maint-info">
                    <div class="maint-mid">${slot.machine_id} <span class="badge badge-${slot.priority || 'medium'}">${(slot.priority || 'medium').toUpperCase()}</span></div>
                    <div class="maint-time">Scheduled: ${timeStr}</div>
                    <div class="maint-reason" title="${slot.reason}">${slot.reason}</div>
                </div>
            </div>
        `;
    }).join('');
}

function addAlertItem(alert) {
    alerts.unshift(alert);
    if (alerts.length > 100) alerts = alerts.slice(0, 100);
    document.getElementById('alert-count').textContent = alerts.length;
    const container = document.getElementById('alert-log');
    const empty = container.querySelector('.empty-state');
    if (empty) empty.remove();

    const time = new Date(alert.timestamp);
    const timeStr = time.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const prioCls = `badge-${alert.priority || 'info'}`;
    const isDataLink = alert.sensors_affected && alert.sensors_affected.includes('_data_link');
    const isLlm = alert.is_llm;

    const item = document.createElement('div');
    item.className = 'alert-item';
    item.innerHTML = `
        <div class="alert-meta">
            <span class="alert-time">${timeStr}</span>
            <span class="badge ${prioCls}">${(alert.priority || 'info').toUpperCase()}</span>
            <span class="alert-mid">${alert.machine_id}</span>
            ${isDataLink ? '<span class="badge badge-fault">DATA LINK</span>' : ''}
            <span class="reasoning-tag ${isLlm ? 'tag-ai' : 'tag-rule'}">${isLlm ? 'AI REASONING' : 'RULE-BASED'}</span>
        </div>
        <div class="alert-reason">${alert.reason_summary}</div>
        <div class="alert-llm">${alert.llm_reasoning}</div>
    `;
    container.prepend(item);
    while (container.children.length > 80) container.removeChild(container.lastChild);
}

function connectSSE() {
    const statusEl = document.getElementById('system-status');
    const connEl = document.getElementById('connection-status');
    connEl.className = 'conn-dot conn-connecting';

    const source = new EventSource('/agent/events');

    source.addEventListener('open', () => connEl.className = 'conn-dot conn-connected');
    source.addEventListener('error', () => connEl.className = 'conn-dot conn-disconnected');

    source.addEventListener('system', (e) => {
        const data = JSON.parse(e.data);
        if (data.status === 'active') {
            statusEl.textContent = 'ACTIVE';
            statusEl.className = 'status-badge status-active';
            if(data.baseline_samples) document.getElementById('metric-baselines').textContent = `Computed (${data.baseline_samples} samples/machine)`;
        } else {
            statusEl.textContent = 'INITIALIZING';
            statusEl.className = 'status-badge status-init';
        }
    });

    source.addEventListener('heartbeat', (e) => {
        const data = JSON.parse(e.data);
        const u = data.uptime_seconds;
        const h = Math.floor(u / 3600); const m = Math.floor((u % 3600) / 60); const s = u % 60;
        document.getElementById('metric-uptime').textContent = h > 0 ? `${h}h ${m}m` : `${m}m ${s}s`;
        document.getElementById('metric-suppressed').textContent = data.total_suppressed;
        
        const typesEl = document.getElementById('metric-types');
        if (data.active_anomaly_types.length === 0) {
            typesEl.textContent = 'None'; typesEl.style.color = 'var(--green)';
        } else {
            typesEl.innerHTML = data.active_anomaly_types.map(t => `<span class="anomaly-type-badge at-${t}" style="margin:0 2px">${t.toUpperCase()}</span>`).join('');
        }
    });

    source.addEventListener('reading', (e) => {
        const data = JSON.parse(e.data);
        const mid = data.machine_id;
        const st = machineState[mid];
        st.readings = { temperature_C: data.temperature_C, vibration_mm_s: data.vibration_mm_s, rpm: data.rpm, current_A: data.current_A };
        st.baselines = data.baselines || {};
        st.anomalies = data.active_anomalies || {};
        st.riskScore = data.risk_score;
        st.status = data.status;
        st.dataGap = false;
        st.anomalyType = data.anomaly_type || 'none';
        st.suppressed = data.suppressed_spikes || 0;
        updateMachineCard(mid);
    });

    source.addEventListener('alert', (e) => {
        const data = JSON.parse(e.data);
        addAlertItem(data);
        if (data.sensors_affected && data.sensors_affected.includes('_data_link')) {
            const mid = data.machine_id;
            if (data.reason_summary.includes('Data gap detected')) machineState[mid].dataGap = true;
            else if (data.reason_summary.includes('restored')) machineState[mid].dataGap = false;
            updateMachineCard(mid);
        }
    });

    source.addEventListener('maintenance', (e) => {
        const data = JSON.parse(e.data);
        if (!maintenanceSlots.find(s => s.slot_id === data.slot_id)) {
            maintenanceSlots.unshift(data);
            updateMaintenanceSchedule();
        }
    });

    setInterval(async () => {
        try {
            const resp = await fetch('/api/priority-queue');
            if (resp.ok) { priorityQueue = await resp.json(); updatePriorityQueue(); }
        } catch (_) {}
    }, 2000); // Faster polling to catch escalations smoothly
}

async function initialLoad() {
    try {
        const [alertsResp, pqResp, maintResp] = await Promise.all([fetch('/api/alerts'), fetch('/api/priority-queue'), fetch('/api/maintenance')]);
        if (alertsResp.ok) { alerts = await alertsResp.json(); alerts.forEach(a => addAlertItem(a)); }
        if (pqResp.ok) { priorityQueue = await pqResp.json(); updatePriorityQueue(); }
        if (maintResp.ok) { maintenanceSlots = await maintResp.json(); updateMaintenanceSchedule(); }
    } catch (e) { console.warn('Initial load failed, waiting for SSE:', e); }
}

document.addEventListener('DOMContentLoaded', () => {
    initMachineCards();
    MACHINE_IDS.forEach(mid => updateMachineCard(mid));
    initialLoad();
    connectSSE();
});
