/**
 * VyomaCast Dashboard — Application Logic
 *
 * State management via normalized Map<cluster_id, ClusterState>.
 * XSS-safe: all dynamic text insertion via .textContent only.
 * WebSocket with exponential-backoff auto-reconnect.
 */

// ── Configuration ──────────────────────────────────────────────────────────
const host = window.location.hostname;
const CONFIG = {
  API_BASE: `http://${host}:8000/api/v1`,
  WS_URL: `ws://${host}:8000/ws/updates`,
  RECONNECT_BASE_MS: 1000,
  RECONNECT_MAX_MS: 30000,
  RECONNECT_MULTIPLIER: 2,
  MAX_CLUSTERS: 300,
};

// ── State ──────────────────────────────────────────────────────────────────
// Normalized store: Map<string, ClusterState>
// ClusterState: { id, label, article_count, top_sources, status, created_at, updated_at, last_ws_update }
const clusterStore = new Map();
const processedArticles = new Set();
let ws = null;
let reconnectAttempts = 0;
let reconnectTimer = null;

// ── DOM References ─────────────────────────────────────────────────────────
const dom = {
  grid: document.getElementById('cluster-grid'),
  statusIndicator: document.getElementById('ws-status'),
  statusDot: document.getElementById('ws-status-dot'),
  statusLabel: document.getElementById('ws-status-label'),
  clusterCount: document.getElementById('stat-clusters'),
  articleCount: document.getElementById('stat-articles'),
};

// ── Initialization ─────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  renderSkeletons(6);
  fetchInitialClusters();
  connectWebSocket();

  // Live timestamp updates
  setInterval(updateAllTimestamps, 30000);
});

// ── API: Fetch Initial Clusters ────────────────────────────────────────────
async function fetchInitialClusters() {
  try {
    const res = await fetch(`${CONFIG.API_BASE}/clusters?limit=100&offset=0`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const clusters = await res.json();

    // Replace local state safely on re-fetches
    clusterStore.clear();
    
    // Populate the store
    for (const c of clusters) {
      clusterStore.set(c.id, {
        id: c.id,
        title: c.title || c.label || "Unknown Title",
        article_count: c.article_count,
        top_sources: c.top_sources || [],
        status: c.status,
        created_at: c.created_at,
        updated_at: c.updated_at,
        last_ws_update: null,
      });
    }

    renderFullGrid();
    updateHeaderStats();
  } catch (err) {
    console.error('[VyomaCast] Failed to fetch clusters:', err);
    // API Failure Handling with retry fallback
    renderEmptyState('Unable to load data', true);
  }
}

// ── Rendering: Full Grid ───────────────────────────────────────────────────
function renderFullGrid() {
  dom.grid.innerHTML = '';

  if (clusterStore.size === 0) {
    renderEmptyState('No clusters yet. Waiting for incoming articles…');
    return;
  }

  // Sort by most recently updated first
  const sorted = [...clusterStore.values()].sort((a, b) => {
    const tA = a.last_ws_update || a.updated_at;
    const tB = b.last_ws_update || b.updated_at;
    return new Date(tB) - new Date(tA);
  });

  for (const cluster of sorted) {
    const card = createCardElement(cluster);
    dom.grid.appendChild(card);
  }
}

// ── Rendering: Single Card (XSS-safe) ──────────────────────────────────────
function createCardElement(cluster) {
  const card = document.createElement('div');
  card.className = 'cluster-card';
  card.id = `card-${cluster.id}`;
  card.dataset.clusterId = cluster.id;

  // Header row
  const header = document.createElement('div');
  header.className = 'cluster-card__header';

  const title = document.createElement('h3');
  title.className = 'cluster-card__title';
  title.textContent = cluster.title; // XSS-safe

  const countBadge = document.createElement('span');
  countBadge.className = 'cluster-card__count';
  countBadge.textContent = cluster.article_count;

  header.appendChild(title);
  header.appendChild(countBadge);

  // Meta row
  const meta = document.createElement('div');
  meta.className = 'cluster-card__meta';

  const sourcesContainer = document.createElement('div');
  sourcesContainer.className = 'cluster-card__sources';
  renderSourceTags(sourcesContainer, cluster.top_sources);

  const timeEl = document.createElement('span');
  timeEl.className = 'cluster-card__time';
  timeEl.textContent = formatRelativeTime(cluster.updated_at);

  meta.appendChild(sourcesContainer);
  meta.appendChild(timeEl);

  card.appendChild(header);
  card.appendChild(meta);

  return card;
}

function renderSourceTags(container, sources) {
  container.innerHTML = '';
  const displaySources = (sources || []).slice(0, 4);
  for (const src of displaySources) {
    const tag = document.createElement('span');
    tag.className = 'source-tag';
    tag.textContent = src; // XSS-safe
    container.appendChild(tag);
  }
  if (sources && sources.length > 4) {
    const more = document.createElement('span');
    more.className = 'source-tag';
    more.textContent = `+${sources.length - 4}`;
    container.appendChild(more);
  }
}

// ── Rendering: Skeletons ───────────────────────────────────────────────────
function renderSkeletons(count) {
  dom.grid.innerHTML = '';
  for (let i = 0; i < count; i++) {
    const skel = document.createElement('div');
    skel.className = 'skeleton-card';
    skel.innerHTML = `
      <div class="skeleton-line skeleton-line--title"></div>
      <div class="skeleton-line skeleton-line--short"></div>
      <div class="skeleton-line skeleton-line--tags"></div>
    `;
    dom.grid.appendChild(skel);
  }
}

// ── Rendering: Empty State ─────────────────────────────────────────────────
function renderEmptyState(message, showRetry = false) {
  dom.grid.innerHTML = '';
  const empty = document.createElement('div');
  empty.className = 'empty-state';

  const icon = document.createElement('div');
  icon.className = 'empty-state__icon';
  icon.textContent = '📡';

  const text = document.createElement('p');
  text.className = 'empty-state__text';
  text.textContent = message; // XSS-safe

  empty.appendChild(icon);
  empty.appendChild(text);

  if (showRetry) {
    const btn = document.createElement('button');
    btn.textContent = 'Retry';
    btn.style.marginTop = '15px';
    btn.style.padding = '8px 16px';
    btn.style.background = 'var(--accent-cyan)';
    btn.style.color = '#000';
    btn.style.border = 'none';
    btn.style.borderRadius = '4px';
    btn.style.cursor = 'pointer';
    btn.onclick = () => {
      renderSkeletons(6);
      fetchInitialClusters();
    };
    empty.appendChild(btn);
  } else {
    const sub = document.createElement('p');
    sub.className = 'empty-state__sub';
    sub.textContent = 'The dashboard will update automatically when events arrive.';
    empty.appendChild(sub);
  }

  dom.grid.appendChild(empty);
}

// ── WebSocket: Connection ──────────────────────────────────────────────────
function connectWebSocket() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }

  setWsStatus('reconnecting');
  ws = new WebSocket(CONFIG.WS_URL);

  ws.onopen = () => {
    console.log('[VyomaCast] WebSocket connected');
    
    // WebSocket Reconnect State Resync
    if (reconnectAttempts > 0) {
      console.log('[VyomaCast] Resyncing state... Fetching clusters.');
      fetchInitialClusters();
    }
    
    reconnectAttempts = 0;
    setWsStatus('connected');
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.event === 'cluster_update') {
        handleClusterUpdate(msg.data);
      }
    } catch (err) {
      console.error('[VyomaCast] Failed to parse WS message:', err);
    }
  };

  ws.onclose = (event) => {
    console.warn('[VyomaCast] WebSocket closed:', event.code, event.reason);
    setWsStatus('disconnected');
    scheduleReconnect();
  };

  ws.onerror = (err) => {
    console.error('[VyomaCast] WebSocket error:', err);
    // onclose will fire after onerror, triggering reconnect
  };
}

// ── WebSocket: Exponential Backoff Reconnect ───────────────────────────────
function scheduleReconnect() {
  if (reconnectTimer) clearTimeout(reconnectTimer);

  const delay = Math.min(
    CONFIG.RECONNECT_BASE_MS * Math.pow(CONFIG.RECONNECT_MULTIPLIER, reconnectAttempts),
    CONFIG.RECONNECT_MAX_MS
  );
  reconnectAttempts++;

  console.log(`[VyomaCast] Reconnecting in ${delay}ms (attempt ${reconnectAttempts})…`);
  setWsStatus('reconnecting');

  reconnectTimer = setTimeout(() => {
    connectWebSocket();
  }, delay);
}

// ── WebSocket: Status Indicator ────────────────────────────────────────────
function setWsStatus(state) {
  const el = dom.statusIndicator;

  el.classList.remove('ws-status--connected', 'ws-status--reconnecting', 'ws-status--disconnected');
  el.classList.add(`ws-status--${state}`);

  const labels = {
    connected: 'Live',
    reconnecting: 'Reconnecting',
    disconnected: 'Offline',
  };
  dom.statusLabel.textContent = labels[state] || state;
}

// ── Event Handler: cluster_update ──────────────────────────────────────────
function handleClusterUpdate(data) {
  const { cluster_id, article_id, title, source_domain, is_new_cluster } = data;
  
  // Duplicate Event Protection
  if (article_id) {
    if (processedArticles.has(article_id)) return;
    processedArticles.add(article_id);
    
    // Safety bound to prevent infinite memory growth of tracking Set
    if (processedArticles.size > 10000) {
      const firstItems = Array.from(processedArticles).slice(0, 1000);
      for (const id of firstItems) processedArticles.delete(id);
    }
  }

  const now = new Date().toISOString();

  if (is_new_cluster) {
    // Create a brand new cluster entry
    const newCluster = {
      id: cluster_id,
      title: title,
      article_count: 1,
      top_sources: [source_domain],
      status: 'active',
      created_at: now,
      updated_at: now,
      last_ws_update: now,
    };
    clusterStore.set(cluster_id, newCluster);

    // Remove empty state if present
    const emptyState = dom.grid.querySelector('.empty-state');
    if (emptyState) emptyState.remove();

    // Prepend new card with entrance animation
    const card = createCardElement(newCluster);
    card.classList.add('cluster-card--enter');
    dom.grid.prepend(card);

    // Clean up animation class
    card.addEventListener('animationend', () => {
      card.classList.remove('cluster-card--enter');
    }, { once: true });

  } else {
    // Update existing cluster
    const existing = clusterStore.get(cluster_id);
    if (existing) {
      existing.article_count += 1;
      existing.updated_at = now;
      existing.last_ws_update = now;

      // Add source_domain if not already tracked
      if (source_domain && !existing.top_sources.includes(source_domain)) {
        existing.top_sources.push(source_domain);
      }

      // Surgically update only the affected DOM card
      updateCardDOM(cluster_id, existing);

      // Move card to front (most recent)
      moveCardToFront(cluster_id);
    } else {
      // Cluster not in store yet (missed initial fetch) — treat as new
      handleClusterUpdate({ ...data, is_new_cluster: true });
      return;
    }
  }

  updateHeaderStats();
  enforceMemoryBound();
}

// ── Memory Management ──────────────────────────────────────────────────────
function enforceMemoryBound() {
  if (clusterStore.size <= CONFIG.MAX_CLUSTERS) return;

  // Find oldest clusters based on last time
  const sorted = [...clusterStore.values()].sort((a, b) => {
    const tA = a.last_ws_update || a.updated_at;
    const tB = b.last_ws_update || b.updated_at;
    return new Date(tA) - new Date(tB);
  });

  const excess = sorted.slice(0, clusterStore.size - CONFIG.MAX_CLUSTERS);
  for (const cluster of excess) {
    clusterStore.delete(cluster.id);
    const card = document.getElementById(`card-${cluster.id}`);
    if (card) card.remove();
  }
}

// ── DOM: Surgical Card Update ──────────────────────────────────────────────
function updateCardDOM(clusterId, cluster) {
  const card = document.getElementById(`card-${clusterId}`);
  if (!card) return;

  // Update article count badge
  const countBadge = card.querySelector('.cluster-card__count');
  if (countBadge) {
    countBadge.textContent = cluster.article_count;
  }

  // Update source tags
  const sourcesContainer = card.querySelector('.cluster-card__sources');
  if (sourcesContainer) {
    renderSourceTags(sourcesContainer, cluster.top_sources);
  }

  // Update time
  const timeEl = card.querySelector('.cluster-card__time');
  if (timeEl) {
    timeEl.textContent = formatRelativeTime(cluster.updated_at);
  }

  // Flash animation
  card.classList.remove('cluster-card--flash');
  // Force reflow to restart animation
  void card.offsetWidth;
  card.classList.add('cluster-card--flash');
  card.addEventListener('animationend', () => {
    card.classList.remove('cluster-card--flash');
  }, { once: true });
}

// ── DOM: Move Card to Front ────────────────────────────────────────────────
function moveCardToFront(clusterId) {
  const card = document.getElementById(`card-${clusterId}`);
  if (!card || card === dom.grid.firstElementChild) return;
  dom.grid.prepend(card);
}

// ── Header Stats ───────────────────────────────────────────────────────────
function updateHeaderStats() {
  dom.clusterCount.textContent = clusterStore.size;
  let totalArticles = 0;
  for (const c of clusterStore.values()) {
    totalArticles += c.article_count;
  }
  dom.articleCount.textContent = totalArticles;
}

// ── Live Timestamps ────────────────────────────────────────────────────────
function updateAllTimestamps() {
  for (const cluster of clusterStore.values()) {
    const timeEl = document.querySelector(`#card-${cluster.id} .cluster-card__time`);
    if (timeEl) {
      timeEl.textContent = formatRelativeTime(cluster.updated_at);
    }
  }
}

// ── Utilities ──────────────────────────────────────────────────────────────
function formatRelativeTime(isoString) {
  if (!isoString) return '';
  const now = Date.now();
  const then = new Date(isoString).getTime();
  const diffSec = Math.floor((now - then) / 1000);

  if (diffSec < 5) return 'just now';
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.floor(diffHr / 24);
  return `${diffDay}d ago`;
}
