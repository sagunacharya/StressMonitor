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
  sensorConnected: null,
  lastReadingAt: null,
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
  if (status !== 'Stressed' && status !== 'Not Stressed') {
    pill.className = 'status-pill status-ready';
    text.textContent = state.session ? 'WAITING' : 'READY';
    return;
  }
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
  if (!reading || reading.timestamp == null || !Number.isFinite(Number(reading.timestamp)) ||
      Number(reading.timestamp) <= 0 || !['Stressed', 'Not Stressed'].includes(reading.stress_status)) return;
  reading.timestamp = Number(reading.timestamp);
  state.lastReadingAt = Number(reading.timestamp);
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

function clearLiveReadings() {
  state.latest = null;
  state.lastReadingAt = null;
  updateMetrics(null, null);
  updateStatusPill(null);
  document.getElementById('lastUpdate').textContent = '—';
  document.getElementById('bufferCount').textContent = '0 readings buffered this session';
  showProbLine(null);
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
  state.sensorConnected = connected === true;
  const banner = document.getElementById('progressBanner');
  if (connected === false) {
    banner.style.display = 'block';
    banner.textContent = 'ESP32 disconnected — live readings are paused until the sensor link returns.';
    if (state.latest) {
      document.getElementById('statusPill').className = 'status-pill status-warming';
      document.getElementById('statusText').textContent = 'STALE DATA';
    }
  } else if (connected === true && state.session) {
    showProgress('ESP32 reconnected — waiting for a fresh valid window. Any previously displayed value is not live.');
    if (state.latest) {
      document.getElementById('statusPill').className = 'status-pill status-warming';
      document.getElementById('statusText').textContent = 'STALE DATA';
    }
  } else if (connected === true) {
    hideProgress();
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
  const probability = Number(prob);
  const cutoff = Number(threshold);
  if (!el) return;
  if (prob == null || threshold == null || !Number.isFinite(probability) ||
      !Number.isFinite(cutoff) || probability < 0 || probability > 1 || cutoff < 0 || cutoff > 1) {
    el.textContent = '';
    el.style.display = 'none';
    return;
  }
  el.textContent = `Model stress score: ${(probability * 100).toFixed(1)}% · decision threshold: ${(cutoff * 100).toFixed(1)}%`;
  el.style.display = 'block';
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
      let msg;
      try { msg = JSON.parse(event.data); } catch { return; }
      if (!msg || typeof msg.type !== 'string') return;

      if (msg.type === 'history') {
        state.history = Array.isArray(msg.data) ? msg.data.slice(-300) : [];
        state.session = msg.session || null;
        state.latest = state.history.length ? state.history[state.history.length - 1] : null;
        state.alerts = [];
        let priorStatus = null;
        state.history.forEach((reading) => {
          const currentStatus = reading.stress_status;
          if ((currentStatus === 'Stressed' || currentStatus === 'Not Stressed') &&
              priorStatus !== null && currentStatus !== priorStatus) {
            state.alerts.push({ timestamp: Number(reading.timestamp), status: currentStatus });
          }
          if (currentStatus === 'Stressed' || currentStatus === 'Not Stressed') priorStatus = currentStatus;
        });
        state.alerts = state.alerts.slice(-50);
        renderAlerts();
        document.getElementById('bufferCount').textContent = `${state.history.length} readings buffered this session`;
        updateSessionUI();
        if (state.latest) {
          updateMetrics(state.latest, state.history[state.history.length - 6]);
          updateStatusPill(state.latest.stress_status);
          state.lastStatus = state.latest.stress_status || 'Not Stressed';
          state.lastReadingAt = Number(state.latest.timestamp) || null;
          const updateEl = document.getElementById('lastUpdate');
          updateEl.textContent = state.lastReadingAt ? new Date(state.lastReadingAt * 1000).toLocaleTimeString() : '—';
        } else {
          clearLiveReadings();
        }
      } else if (msg.type === 'esp32_status') {
        setEsp32Status(msg.connected);
      } else if (msg.type === 'session_status') {
        if (msg.state === 'ready') {
          if (!msg.session_id || state.session?.id === msg.session_id) {
            state.session = null;
            state.history = [];
            clearLiveReadings();
            updateSessionUI();
          }
          setReadyStatus();
        } else if (msg.state === 'collecting') {
          collectingSamples = Number(msg.samples_collected) || 0;
          const needed = Number(msg.samples_needed) || 3840;
          showProgress(`Collecting data: ${collectingSamples} / ${needed} samples`);
        } else if (msg.state === 'calibrating') {
          setWarmingStatus(Number(msg.windows_seen) || 0, Number(msg.windows_needed) || 3);
        } else if (msg.state === 'calibration_failed') {
          showProgress('Calibration failed: baseline variation is too small. Stop, check contact and signal quality, then restart in a quiet/resting setup.');
        } else if (msg.state === 'prediction_available') {
          showProgress('Baseline calibration is complete. Waiting for the next fresh, valid prediction window.');
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
        showProgress(msg.reason === 'excessive_motion'
          ? 'Window rejected: excessive motion detected — hold still for a cleaner reading.'
          : 'Window rejected: insufficient PPG/EDA quality or a sampling gap; waiting for a clean window.');
      } else if (msg.type === 'calibration_failed') {
        showProgress('Calibration could not estimate stable baseline variation. Stop, check sensor contact, then start a new quiet/resting session.');
      } else if (msg.type === 'sampling_gap') {
        showProgress(state.sensorConnected === false
          ? 'ESP32 disconnected — partial window discarded; waiting for the serial link to return.'
          : 'Sampling gap detected — partial window discarded; collecting a fresh window.');
      } else if (msg.type === 'sensor_quality') {
        const messages = {
          missing_ppg_data: 'PPG input is missing. Check finger placement, sensor power and I²C wiring.',
          ppg_saturated: 'PPG input is saturated. Check sensor contact and optical conditions.',
          repeated_ppg_values: 'Repeated PPG values detected. The partial window was discarded; checking signal quality.',
        };
        showProgress(messages[msg.reason] || 'Sensor data quality issue detected.');
      } else if (msg.type === 'window_error') {
        showProgress(`Processing failed (${msg.reason || 'unknown stage'}). No prediction was generated.`);
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
  try { const res = await fetch(`${API_URL}/api/auth/logout`, { method: 'POST', credentials: 'include' }); if (!res.ok) throw new Error('logout failed'); } catch { showProgress('Logout could not be confirmed; retry or close the browser session.'); return; }
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
      const res = await fetch(`${API_URL}/api/sessions/${encodeURIComponent(state.session.id)}/stop`, { method: 'POST', credentials: 'include' });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.error || 'Could not stop session');
      state.session = null;
      state.history = [];
      state.alerts = [];
      state.lastStatus = 'Not Stressed';
      renderAlerts();
      clearLiveReadings();
      updateSessionUI();
    } else {
      const acknowledged = window.confirm('Calibration uses the first 3 or more valid windows as your baseline. Before continuing, seat the subject comfortably, keep still, and aim for a quiet/resting state. This condition cannot be verified automatically. Start calibration?');
      if (!acknowledged) return;
      const res = await fetch(`${API_URL}/api/sessions/start`, {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subject_id: 'SUBJECT-01', calibration_acknowledged: true }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.error || 'Could not start session');
      state.session = body;
      state.history = [];
      state.alerts = [];
      clearLiveReadings();
      updateSessionUI();
      showProgress('Calibration started. Keep the subject quiet and still while valid baseline windows are collected.');
    }
  } catch (error) {
    showProgress(error instanceof Error ? error.message : 'Session control failed.');
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
  body.replaceChildren();
  const loading = document.createElement('tr');
  const cell = document.createElement('td'); cell.colSpan = 6; cell.className = 'muted'; cell.textContent = 'Loading…';
  loading.appendChild(cell); body.appendChild(loading);
  try {
    const res = await fetch(`${API_URL}/api/sessions?limit=200`, { credentials: 'include' });
    const sessions = await res.json();
    if (!res.ok) throw new Error(sessions.error || 'Could not load sessions');
    body.replaceChildren();
    if (!Array.isArray(sessions) || sessions.length === 0) {
      const row = document.createElement('tr'); const td = document.createElement('td');
      td.colSpan = 6; td.className = 'muted'; td.textContent = 'No past sessions yet — start one from the Live tab.';
      row.appendChild(td); body.appendChild(row); return;
    }
    sessions.forEach((item) => {
      const tr = document.createElement('tr');
      [fmtDate(item.start_time), item.subject_id, duration(item.start_time, item.end_time),
       String(item.num_readings ?? 0), `${item.stressed_count ?? 0} / ${item.num_readings ?? 0}`].forEach((value, index) => {
        const td = document.createElement('td'); td.textContent = value == null ? '—' : String(value);
        if (index === 1) td.className = 'muted'; tr.appendChild(td);
      });
      const td = document.createElement('td'); const button = document.createElement('button');
      button.className = 'view-btn'; button.textContent = 'View';
      button.addEventListener('click', () => viewSession(item.id)); td.appendChild(button); tr.appendChild(td);
      body.appendChild(tr);
    });
  } catch (error) {
    body.replaceChildren(); const row = document.createElement('tr'); const td = document.createElement('td');
    td.colSpan = 6; td.className = 'muted'; td.textContent = error instanceof Error ? error.message : 'Could not reach backend.';
    row.appendChild(td); body.appendChild(row);
  }
}

async function viewSession(sessionId) {
  const card = document.getElementById('recordsCard');
  const body = document.getElementById('recordsBody');
  card.style.display = 'block'; body.replaceChildren();
  try {
    const res = await fetch(`${API_URL}/api/sessions/${encodeURIComponent(sessionId)}/readings?limit=1000`, { credentials: 'include' });
    const readings = await res.json();
    if (!res.ok) throw new Error(readings.error || 'Could not load readings');
    if (!Array.isArray(readings) || readings.length === 0) {
      const row = document.createElement('tr'); const td = document.createElement('td');
      td.colSpan = 6; td.className = 'muted'; td.textContent = 'No readings recorded for this session.';
      row.appendChild(td); body.appendChild(row); return;
    }
    readings.forEach((reading) => {
      const tr = document.createElement('tr');
      [fmtTime(reading.timestamp), reading.hr, reading.sdnn, reading.rmssd, reading.eda_mean].forEach((value, index) => {
        const td = document.createElement('td');
        td.textContent = value == null || !Number.isFinite(Number(value)) ? '—' : index === 0 ? String(value) : Number(value).toFixed(2);
        if (index === 0) td.className = 'muted'; tr.appendChild(td);
      });
      const status = reading.stress_status || (reading.stress_label === 1 ? 'Stressed' : reading.stress_label === 0 ? 'Not Stressed' : 'Unknown');
      const td = document.createElement('td'); const badge = document.createElement('span');
      badge.className = `badge ${status === 'Stressed' ? 'stressed' : 'calm'}`; badge.textContent = status;
      td.appendChild(badge); tr.appendChild(td); body.appendChild(tr);
    });
  } catch (error) {
    body.replaceChildren(); const row = document.createElement('tr'); const td = document.createElement('td');
    td.colSpan = 6; td.className = 'muted'; td.textContent = error instanceof Error ? error.message : 'Could not reach backend.';
    row.appendChild(td); body.appendChild(row);
  }
}
window.viewSession = viewSession;

setInterval(() => {
  if (state.session && state.latest && state.lastReadingAt && Date.now() / 1000 - state.lastReadingAt > 90) {
    document.getElementById('statusPill').className = 'status-pill status-warming';
    document.getElementById('statusText').textContent = 'STALE DATA';
    showProgress('No recent prediction has arrived. Check the serial connection and signal quality; the last value is not live.');
  }
}, 5000);
