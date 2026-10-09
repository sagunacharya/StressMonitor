// ---------------------------------------------------------------------------
// Host detection: never hardcodes "localhost". Works whether this page is
// opened as localhost, 127.0.0.1, or your machine's network name/IP, since
// the browser already knows what host it was loaded from.
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// Host detection: never hardcodes "localhost". Works whether this page is
// opened as localhost, 127.0.0.1, your machine's LAN IP, or a public tunnel
// URL (e.g. ngrok) -- only adds ":port" when the browser's URL actually has
// one (default ports for http/https are implicit and must NOT be forced).
// ---------------------------------------------------------------------------
const HOST = window.location.hostname || 'localhost';
const PORT = window.location.port; // '' when on default 80/443 (e.g. behind ngrok)
const PORT_SUFFIX = PORT ? `:${PORT}` : '';
const API_URL = `${window.location.protocol}//${HOST}${PORT_SUFFIX}`;
const WS_URL = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${HOST}${PORT_SUFFIX}/ws/stream`;

const METRICS = [
  { key: 'hr', label: 'Heart Rate', unit: 'bpm', color: 'var(--rose)', row: 1, icon: '❤' },
  { key: 'eda_mean', label: 'EDA Mean', unit: 'µS', color: 'var(--orange)', row: 1, icon: '⚡' },
  { key: 'sdnn', label: 'SDNN', unit: 'ms', color: 'var(--indigo)', row: 1 },
  { key: 'rmssd', label: 'RMSSD', unit: 'ms', color: 'var(--purple)', row: 1 },
  { key: 'eda_std', label: 'EDA Std', unit: 'µS', color: 'var(--teal)', row: 1 },
  { key: 'eda_min', label: 'EDA Min', unit: 'µS', color: 'var(--yellow)', row: 2 },
  { key: 'eda_max', label: 'EDA Max', unit: 'µS', color: 'var(--pink)', row: 2 },
  { key: 'eda_range', label: 'EDA Range', unit: 'µS', color: 'var(--cyan)', row: 2 },
  { key: 'eda_slope', label: 'EDA Slope', unit: 'µS/s', color: 'var(--lime)', row: 2 },
];

const state = {
  history: [],
  latest: null,
  session: null,
  lastStatus: 'Not Stressed',
  alerts: [],
};

// ---------------------------------------------------------------------------
// Build metric cards once -- split across two explicit rows
// ---------------------------------------------------------------------------
const metricsGridRow1 = document.getElementById('metricsGridRow1');
const metricsGridRow2 = document.getElementById('metricsGridRow2');
METRICS.forEach((m) => {
  const card = document.createElement('div');
  card.className = 'metric-card-uniform';
  card.style.setProperty('--accent', m.color);
  card.id = `card-${m.key}`;

  const statusTag = m.row === 1 ? `<span class="status-tag" id="status-${m.key}">NORMAL</span>` : '';
  const icon = m.icon ? `<div class="mcu-icon">${m.icon}</div>` : '';

  card.innerHTML = `
    ${icon}
    <div class="mcu-body">
      <div class="metric-label">
        <span>${m.label}</span>
        ${statusTag}
      </div>
      <div class="metric-value">
        <span class="num" id="value-${m.key}">—</span>
        <span class="unit">${m.unit}</span>
        <span class="metric-trend" id="trend-${m.key}"></span>
      </div>
    </div>`;
  (m.row === 1 ? metricsGridRow1 : metricsGridRow2).appendChild(card);
});

function updateMetrics(latest, prev) {
  const stressed = latest?.stress_status === 'Stressed';
  METRICS.forEach((m) => {
    const valueEl = document.getElementById(`value-${m.key}`);
    const trendEl = document.getElementById(`trend-${m.key}`);
    const statusEl = document.getElementById(`status-${m.key}`);
const value = latest?.[m.key];
valueEl.textContent =
    value == null || !Number.isFinite(Number(value))
        ? '—'
        : Number(value).toFixed(2);    


    if (statusEl) {
      statusEl.textContent = stressed ? 'HIGH' : 'NORMAL';
      statusEl.className = `status-tag ${stressed ? 'high' : ''}`;
    }
    if (latest && prev) {
      const diff = latest[m.key] - prev[m.key];
      if (Math.abs(diff) >= 0.05) {
        trendEl.textContent = `${diff >= 0 ? '▲' : '▼'} ${Math.abs(diff).toFixed(1)}`;
        trendEl.className = `metric-trend ${diff >= 0 ? 'up' : 'down'}`;
      } else {
        trendEl.textContent = '';
      }
    }
  });
}

function updateStatusPill(status) {
  const pill = document.getElementById('statusPill');
  const text = document.getElementById('statusText');
  const stressed = status === 'Stressed';
  pill.className = `status-pill ${stressed ? 'status-stressed' : 'status-calm'}`;
  text.textContent = stressed ? 'STRESSED' : 'NOT STRESSED';
}

function timeAgo(ts) {
  const diff = Math.max(0, Date.now() / 1000 - ts);
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  return `${Math.floor(diff / 60)}m ago`;
}

function renderAlerts() {
  const log = document.getElementById('eventLog');
  if (state.alerts.length === 0) {
    log.innerHTML = '<li class="event-empty">No transitions yet — status is stable.</li>';
    return;
  }
  log.innerHTML = state.alerts.slice().reverse().map((a) => {
    const stressed = a.status === 'Stressed';
    return `
      <li>
        <span class="event-dot ${stressed ? 'stressed' : 'calm'}"></span>
        <span>Status changed to <strong>${a.status}</strong></span>
        <span class="event-time">${timeAgo(a.timestamp)}</span>
      </li>`;
  }).join('');
}

function handleReading(reading) {
  state.history.push(reading);
  if (state.history.length > 300) state.history.shift();
  const prev = state.history[state.history.length - 6];
  state.latest = reading;

  updateMetrics(reading, prev);
  updateStatusPill(reading.stress_status);

  const lastUpdateEl = document.getElementById('lastUpdate');
  if (lastUpdateEl) lastUpdateEl.textContent = new Date(reading.timestamp * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

  if (reading.stress_status !== state.lastStatus) {
    state.alerts.push({ timestamp: reading.timestamp, status: reading.stress_status });
    if (state.alerts.length > 50) state.alerts.shift();
    renderAlerts();
  }
  state.lastStatus = reading.stress_status;

  document.getElementById('bufferCount').textContent = `${state.history.length} readings buffered this session`;
}

// ---------------------------------------------------------------------------
// Connection status UI
// ---------------------------------------------------------------------------
function setConnStatus(status) {
  const dot = document.getElementById('connDot');
  const label = document.getElementById('connLabel');
  dot.className = 'conn-dot';
  if (status === 'live') { dot.classList.add('live'); label.textContent = 'LIVE'; }
  else if (status === 'reconnecting') { dot.classList.add('warn'); label.textContent = 'RECONNECTING'; }
  else { label.textContent = 'CONNECTING'; }
}

function updateSessionUI() {
  const subjectLabel = document.getElementById('subjectLabel');
  const btn = document.getElementById('sessionBtn');
  subjectLabel.textContent = state.session?.subject_id ?? '—';
  if (state.session) {
    btn.textContent = 'Stop Session';
    btn.className = 'btn btn-stop';
  } else {
    btn.textContent = 'Start Session';
    btn.className = 'btn btn-start';
    setReadyStatus();
  }
}

// ---------------------------------------------------------------------------
// NOTE: there is deliberately no fake/simulated-data fallback here. If the
// backend or ESP32 is unreachable, the UI shows RECONNECTING / a status
// banner and keeps retrying -- it never fabricates readings. The system's
// spec requires every value shown to come from real ESP32 sensor data
// processed by the real model, with no exceptions for disconnects.
// ---------------------------------------------------------------------------

function setEsp32Status(connected) {
  const banner = document.getElementById('progressBanner');
  if (connected === false) {
    banner.style.display = 'block';
    banner.textContent = 'ESP32 not connected — waiting for sensor link...';
  }
}

function showProgress(text) {
  const banner = document.getElementById('progressBanner');
  banner.style.display = 'block';
  banner.textContent = text;
}

function hideProgress() {
  document.getElementById('progressBanner').style.display = 'none';
}

function showProbLine(prob, threshold) {
    const el = document.getElementById('probLine');
    if (el) el.style.display = 'none';
}

function setReadyStatus() {
  const pill = document.getElementById('statusPill');
  const text = document.getElementById('statusText');
  pill.className = 'status-pill status-ready';
  text.textContent = 'READY';
  hideProgress();
  showProbLine(null);
}

function setWarmingStatus(windowsSeen, windowsNeeded) {
  const pill = document.getElementById('statusPill');
  const text = document.getElementById('statusText');
  pill.className = 'status-pill status-warming';
  text.textContent = 'CALIBRATING';
  showProgress(`Establishing session baseline: window ${windowsSeen} / ${windowsNeeded}`);
}

// ---------------------------------------------------------------------------
// WebSocket connection
// ---------------------------------------------------------------------------
let ws = null;
let collectingSamples = 0;

function connect() {
  try {
    ws = new WebSocket(WS_URL);

    ws.onopen = () => {
      setConnStatus('live');
    };

    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);

      if (msg.type === 'history') {
        state.history = msg.data.slice(-300);
        if (msg.session) { state.session = msg.session; updateSessionUI(); }
        const last = state.history[state.history.length - 1];
        if (last) { updateMetrics(last, state.history[state.history.length - 6]); updateStatusPill(last.stress_status); state.lastStatus = last.stress_status; }

      } else if (msg.type === 'esp32_status') {
        setEsp32Status(msg.connected);

      } else if (msg.type === 'session_status') {
        if (msg.state === 'ready') {
          setReadyStatus();
        } else if (msg.state === 'collecting') {
          collectingSamples = 0;
          showProgress(`Collecting data: 0 / ${msg.samples_needed} samples`);
        }

      } else if (msg.type === 'collecting_progress') {
        collectingSamples = msg.samples_collected;
        const secs = (msg.samples_collected / 64).toFixed(0);
        const totalSecs = (msg.samples_needed / 64).toFixed(0);
        showProgress(`Collecting data: ${msg.samples_collected} / ${msg.samples_needed} samples (${secs}s / ${totalSecs}s)`);

      } else if (msg.type === 'warming_up') {
        setWarmingStatus(msg.windows_seen, msg.windows_needed);

      } else if (msg.type === 'reading') {
        hideProgress();
        showProbLine(msg.stress_prob, msg.decision_threshold);
        handleReading(msg);

      } else if (msg.type === 'window_rejected') {
        showProgress(
          msg.reason === 'excessive_motion'
            ? 'Window rejected: excessive motion detected — hold still for a cleaner reading.'
            : 'Window rejected: signal quality too low for this window.'
        );

      } else if (msg.type === 'window_error') {
        showProgress(`Processing error: ${msg.detail || msg.reason}`);
      }
    };

    ws.onclose = () => {
      setConnStatus('reconnecting');
      setTimeout(connect, 3000);
    };
    ws.onerror = () => ws.close();
  } catch {
    setConnStatus('reconnecting');
    setTimeout(connect, 3000);
  }
}

// ---------------------------------------------------------------------------
// Auth guard -- redirect to /login if no active MySQL-backed session cookie
// ---------------------------------------------------------------------------
async function checkAuth() {
  try {
    const res = await fetch(`${API_URL}/api/auth/me`, { credentials: 'include' });
    const me = await res.json();
    if (!me) { window.location.href = '/login'; return; }
    document.getElementById('usernameLabel').textContent = me.username;
  } catch {
    window.location.href = '/login';
  }
}
checkAuth();

document.getElementById('logoutBtn').addEventListener('click', async () => {
  try { await fetch(`${API_URL}/api/auth/logout`, { method: 'POST', credentials: 'include' }); } catch {}
  window.location.href = '/login';
});

connect();

// ---------------------------------------------------------------------------
// Session controls
// ---------------------------------------------------------------------------
document.getElementById('sessionBtn').addEventListener('click', async () => {
  const isActive = !!state.session;
  const btn = document.getElementById('sessionBtn');
  btn.disabled = true;
  try {
    if (isActive) {
      await fetch(`${API_URL}/api/sessions/${state.session.id}/stop`, { method: 'POST', credentials: 'include' });
      state.session = null;
      state.history = [];
      state.alerts = [];
      state.lastStatus = 'Not Stressed';
      renderAlerts();
      updateSessionUI();
    } else {
      const res = await fetch(`${API_URL}/api/sessions/start?subject_id=SUBJECT-01`, { method: 'POST', credentials: 'include' });
      if (!res.ok) throw new Error('start failed');
      state.session = await res.json();
      state.history = [];
      updateSessionUI();
    }
  } catch {
    console.warn('Backend not reachable for session control.');
    showProgress('Could not reach the backend — session control failed.');
  } finally {
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------
function activateTab(tab) {
  document.querySelectorAll('.tab-btn, .sb-nav-btn[data-tab]').forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.tab-panel').forEach((p) => p.classList.remove('active'));
  document.getElementById(`${tab}Tab`).classList.add('active');
  if (tab === 'history') loadSessions();
}

document.querySelectorAll('.tab-btn, .sb-nav-btn[data-tab]').forEach((btn) => {
  btn.addEventListener('click', () => activateTab(btn.dataset.tab));
});

document.getElementById('alertsNavBtn').addEventListener('click', () => {
  activateTab('live');
  document.getElementById('alerts').scrollIntoView({ behavior: 'smooth' });
});

// ---------------------------------------------------------------------------
// History tab
// ---------------------------------------------------------------------------
function fmtDate(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}
function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}
function duration(start, end) {
  if (!start) return '—';
  const secs = (end || Date.now() / 1000) - start;
  return `${Math.floor(secs / 60)}m`;
}

async function loadSessions() {
  const body = document.getElementById('sessionsBody');
  body.innerHTML = '<tr><td colspan="6" class="muted">Loading…</td></tr>';
  try {
    const res = await fetch(`${API_URL}/api/sessions`, { credentials: 'include' });
    const sessions = await res.json();
    if (sessions.length === 0) {
      body.innerHTML = '<tr><td colspan="6" class="muted">No past sessions yet — start one from the Live tab.</td></tr>';
      return;
    }
    body.innerHTML = sessions.map((s) => `
      <tr>
        <td>${fmtDate(s.start_time)}</td>
        <td class="muted">${s.subject_id}</td>
        <td>${duration(s.start_time, s.end_time)}</td>
        <td>${s.num_readings}</td>
        <td>${s.stressed_count} / ${s.num_readings}</td>
        <td><button class="view-btn" onclick="viewSession('${s.id}')">View</button></td>
      </tr>
    `).join('');
  } catch {
    body.innerHTML = '<tr><td colspan="6" class="muted">Could not reach backend.</td></tr>';
  }
}

async function viewSession(sessionId) {
  const card = document.getElementById('recordsCard');
  const body = document.getElementById('recordsBody');
  card.style.display = 'block';
  body.innerHTML = '<tr><td colspan="6" class="muted">Loading…</td></tr>';
  try {
    const res = await fetch(`${API_URL}/api/sessions/${sessionId}/readings`, { credentials: 'include' });
    const readings = await res.json();
    if (readings.length === 0) {
      body.innerHTML = '<tr><td colspan="6" class="muted">No readings recorded for this session.</td></tr>';
      return;
    }
    body.innerHTML = readings.map((r) => {
      const stressed = r.stress_label === 1;
      return `
        <tr>
          <td class="muted">${fmtTime(r.timestamp)}</td>
          <td>${r.hr}</td><td>${r.sdnn}</td><td>${r.rmssd}</td><td>${r.eda_mean}</td>
          <td><span class="badge ${stressed ? 'stressed' : 'calm'}">${stressed ? 'Stressed' : 'Not Stressed'}</span></td>
        </tr>`;
    }).join('');
  } catch {
    body.innerHTML = '<tr><td colspan="6" class="muted">Could not reach backend.</td></tr>';
  }
}
window.viewSession = viewSession;
