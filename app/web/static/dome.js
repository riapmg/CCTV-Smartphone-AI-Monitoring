(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);

  const el = {
    stage: $('domeStage'),
    ring: $('domeRing'),
    hubFold: $('hubFold'),
    hubSector: $('hubSector'),
    hubAngle: $('hubAngle'),
    hubPause: $('hubPause'),
    foldChips: $('foldChips'),
    foldValue: $('foldValue'),
    speedRange: $('speedRange'),
    speedValue: $('speedValue'),
    pauseBtn: $('pauseBtn'),
    reverseBtn: $('reverseBtn'),
    zeroBtn: $('zeroBtn'),
    angleRange: $('angleRange'),
    angleValue: $('angleValue'),
    refractRange: $('refractRange'),
    refractValue: $('refractValue'),
    ariaLevel: $('ariaLevel'),
    ariaLevelBadge: $('ariaLevelBadge'),
    ariaLog: $('ariaLog'),
    ariaForm: $('ariaForm'),
    ariaInput: $('ariaInput'),
    ariaProvider: $('ariaProvider'),
    monitorCard: $('monitorCard'),
    monitorTitle: $('monitorTitle'),
    monitorPreview: $('monitorPreview'),
    monitorFeed: $('monitorFeed'),
    monitorLabel: $('monitorLabel'),
    monitorUrl: $('monitorUrl'),
    monitorUrlRow: $('monitorUrlRow'),
    cameraFinder: $('cameraFinder'),
    cameraList: $('cameraList'),
    monitorDiscover: $('monitorDiscover'),
    monitorSave: $('monitorSave'),
    monitorSnapshot: $('monitorSnapshot'),
    monitorInspect: $('monitorInspect'),
    monitorClear: $('monitorClear'),
    monitorMeta: $('monitorMeta'),
    monitorClose: $('monitorClose'),
    auditList: $('auditList'),
    auditClear: $('auditClear'),
    appBadge: $('appBadge'),
    linkBadge: $('linkBadge'),
    domeSubtitle: $('domeSubtitle')
  };

  const LEVEL_LABELS = {
    1: 'L1 assisted',
    2: 'L2 semi-autonomous',
    3: 'L3 fully autonomous',
    4: 'L4 bounded self-tuning'
  };

  let feeds = [];
  let state = null;
  let lastData = null;
  let rotation = 0;
  let serverRotation = 0;
  let speed = 0;
  let reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  let selectedId = null;
  let selectedSignature = '';
  let previewTimer = null;
  let lastFrameTs = 0;
  let syncing = false;

  // ---------------------------------------------------------------- utils

  function feedById(id) {
    return feeds.find((feed) => feed.id === id) || null;
  }

  function feedLabel(id) {
    const feed = feedById(id);
    return feed ? feed.label : '';
  }

  function feedKind(id) {
    const feed = feedById(id);
    return feed ? feed.kind : '';
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[ch]));
  }

  function tileUrl(monitorId, width) {
    return `/api/dome/monitors/${monitorId}/tile.jpg?w=${width}&t=${Date.now()}`;
  }

  async function fetchJson(url, options) {
    try {
      const response = await fetch(url, options || {});
      let json = null;
      try { json = await response.json(); } catch (err) { json = null; }
      return { status: response.status, json };
    } catch (err) {
      return { status: 0, json: null, error: String(err) };
    }
  }

  function setSubtitle(text) {
    el.domeSubtitle.textContent = text;
  }

  function signature(domeState) {
    if (!domeState) return '';
    return JSON.stringify([
      domeState.fold,
      domeState.paused,
      domeState.speed_dps,
      domeState.refraction,
      domeState.rotation_deg,
      (domeState.monitors || []).map((m) => [m.id, m.feed, m.label, m.url])
    ]);
  }

  function monitorSignature(monitor) {
    return JSON.stringify([monitor.feed, monitor.label, monitor.url]);
  }

  function monitorState(monitor) {
    if (!monitor.feed) return 'empty';
    const kind = feedKind(monitor.feed);
    if (kind === 'web') return 'web';
    if (kind === 'camera' && !monitor.url) return 'setup';
    const health = lastData && lastData.health ? lastData.health[monitor.feed] : null;
    if (health && health.last_error) return 'error';
    return 'live';
  }

  // ------------------------------------------------------------ data load

  async function load() {
    const result = await fetchJson('/api/dome');
    if (!result.json || !result.json.ok) {
      setSubtitle('Could not load the dome');
      return;
    }
    applyData(result.json.data, true);
  }

  function applyData(data, force) {
    if (!data || !data.state) return;
    const previous = state ? signature(state) : '';
    lastData = data;
    feeds = data.feeds || feeds;
    const incoming = data.state;
    const changed = force || signature(incoming) !== previous;

    state = incoming;

    if (changed) {
      const incomingRotation = Number(state.rotation_deg) || 0;
      if (force || incomingRotation !== serverRotation) {
        rotation = incomingRotation;
      }
      serverRotation = incomingRotation;
      speed = state.paused ? 0 : Number(state.speed_dps) || 0;
      render();
    }

    updateBadges();
    updateDots();

    if (selectedId) {
      const monitor = state.monitors.find((m) => m.id === selectedId);
      if (!monitor) {
        closeMonitor();
      } else if (monitorSignature(monitor) !== selectedSignature) {
        openMonitor(selectedId, true);
      }
    }
  }

  // -------------------------------------------------------------- render

  function render() {
    if (!state) return;
    const sector = Number(state.sector_angle) || (360 / state.fold);
    const refraction = Number(state.refraction) || 0;

    el.stage.style.setProperty('--sector', String(sector));
    el.stage.style.setProperty('--refract', String(refraction / 100));

    el.hubFold.textContent = String(state.fold);
    el.hubSector.textContent = `${sector}° sectors`;
    el.foldValue.textContent = `${state.fold}-fold`;

    el.speedValue.textContent = `${(Number(state.speed_dps) || 0).toFixed(1)}°/s`;
    el.speedRange.value = String(state.speed_dps);
    el.angleRange.value = String(Math.round(Number(state.rotation_deg) || 0));
    el.angleValue.textContent = `${Math.round(Number(state.rotation_deg) || 0)}°`;
    el.refractRange.value = String(refraction);
    el.refractValue.textContent = `${refraction}%`;

    setPausedUi(Boolean(state.paused));
    renderChips();
    buildRing();
    renderAriaStatus(lastData ? lastData.aria : null);
  }

  function setPausedUi(paused) {
    const label = paused ? 'Resume' : 'Pause';
    el.pauseBtn.textContent = label;
    el.hubPause.textContent = label;
    el.hubPause.setAttribute('aria-pressed', String(paused));
  }

  function renderChips() {
    el.foldChips.innerHTML = '';
    (state.fold_choices || []).forEach((choice) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'chip';
      button.textContent = String(choice);
      button.setAttribute('aria-pressed', String(choice === state.fold));
      button.addEventListener('click', () => updateDome({ fold: choice }));
      el.foldChips.appendChild(button);
    });
  }

  function buildRing() {
    el.ring.innerHTML = '';
    const count = state.monitors.length;
    const size = el.stage.clientWidth || 600;
    const rings = count <= 8
      ? [{ count: count, radius: size * 0.36 }]
      : [
          { count: Math.ceil(count / 2), radius: size * 0.40 },
          { count: Math.floor(count / 2), radius: size * 0.225 }
        ];

    let index = 0;
    rings.forEach((ring, ringIndex) => {
      if (!ring.count) return;
      const slot = (2 * Math.PI * ring.radius) / ring.count;
      const width = Math.max(44, Math.min(size * 0.24, slot * 0.88));
      const height = Math.round(width * 0.78);
      const offset = ringIndex === 0 ? 0 : (360 / ring.count) / 2;

      for (let j = 0; j < ring.count; j += 1) {
        const monitor = state.monitors[index];
        if (!monitor) break;

        const node = document.createElement('button');
        node.type = 'button';
        node.className = 'monitor';
        node.dataset.id = monitor.id;
        node.dataset.angle = String((360 / ring.count) * j + offset);
        node.dataset.radius = String(ring.radius);
        node.style.width = `${width}px`;
        node.style.height = `${height}px`;
        node.setAttribute(
          'aria-label',
          `Monitor ${index + 1}: ${monitor.label || feedLabel(monitor.feed) || 'unassigned'}`
        );
        node.innerHTML = `
          <img class="monitor-tile" alt="" src="${tileUrl(monitor.id, 320)}">
          <span class="monitor-state" data-state="${monitorState(monitor)}"></span>
          <span class="monitor-name">${escapeHtml(monitor.label || feedLabel(monitor.feed) || 'Unassigned')}</span>
        `;
        node.addEventListener('click', () => openMonitor(monitor.id));
        el.ring.appendChild(node);
        index += 1;
      }
    });

    applyTransforms();
    highlightSelected();
  }

  function applyTransforms() {
    const nodes = el.ring.children;
    for (let i = 0; i < nodes.length; i += 1) {
      const node = nodes[i];
      const angle = Number(node.dataset.angle) + rotation;
      const radius = Number(node.dataset.radius);
      node.style.transform =
        `translate(-50%, -50%) rotate(${angle}deg) translateY(-${radius}px) rotate(${-angle}deg)`;
    }
    el.hubAngle.textContent = `${Math.round(((rotation % 360) + 360) % 360)}°`;
  }

  function updateDots() {
    if (!state) return;
    const nodes = el.ring.children;
    for (let i = 0; i < nodes.length; i += 1) {
      const monitor = state.monitors.find((m) => m.id === nodes[i].dataset.id);
      const dot = nodes[i].querySelector('.monitor-state');
      if (monitor && dot) dot.dataset.state = monitorState(monitor);
    }
  }

  function updateBadges() {
    if (!lastData) return;
    const app = lastData.app || {};
    el.appBadge.textContent = `ingest: ${app.ingest_enabled ? 'on' : 'off'} · rec: ${app.recording ? 'on' : 'off'}`;
    el.appBadge.dataset.state = app.ingest_enabled ? 'ok' : '';

    const health = lastData.health || {};
    // A CamFlow node with no frames yet is an ingest state, not a broken link.
    const failing = Object.keys(health).filter((key) => (
      health[key] && health[key].last_error && feedKind(key) !== 'stream'
    ));
    el.linkBadge.textContent = failing.length ? `link: ${failing.length} degraded` : 'link: ok';
    el.linkBadge.dataset.state = failing.length ? 'warn' : 'ok';
  }

  function renderAriaStatus(aria) {
    if (!aria) return;
    if (document.activeElement !== el.ariaLevel) {
      el.ariaLevel.value = String(aria.level);
    }
    el.ariaLevelBadge.textContent = LEVEL_LABELS[aria.level] || `L${aria.level}`;
    el.ariaProvider.textContent = aria.configured
      ? `models: ${aria.providers.map((p) => `${p.name}/${p.model}`).join(', ')}`
      : 'No model key configured — local rules only';
  }

  // ------------------------------------------------------------ mutations

  async function updateDome(patch) {
    if (!state) await load();
    if (!state || syncing) return;
    syncing = true;
    const result = await fetchJson('/api/dome', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch)
    });
    syncing = false;

    if (!result.json || !result.json.ok) {
      setSubtitle((result.json && result.json.error) || 'Update failed');
      return;
    }

    const hasAngle = Object.prototype.hasOwnProperty.call(patch, 'rotation_deg');
    state = result.json.data;
    serverRotation = Number(state.rotation_deg) || 0;
    if (hasAngle) rotation = serverRotation;
    speed = state.paused ? 0 : Number(state.speed_dps) || 0;
    render();
    updateDots();
  }

  // --------------------------------------------------------------- monitors

  function openMonitor(id, silent) {
    const monitor = state.monitors.find((m) => m.id === id);
    if (!monitor) {
      closeMonitor();
      return;
    }

    selectedId = id;
    el.monitorCard.hidden = false;
    el.monitorTitle.textContent = `${id} · ${monitor.label || feedLabel(monitor.feed) || 'unassigned'}`;

    el.monitorFeed.innerHTML = '<option value="">Unassigned</option>' + feeds
      .map((feed) => `<option value="${feed.id}">${escapeHtml(feed.label)}</option>`)
      .join('');
    el.monitorFeed.value = monitor.feed || '';
    el.monitorLabel.value = monitor.label || '';
    el.monitorUrl.value = monitor.url || '';
    el.monitorMeta.textContent = '';
    updateUrlRow();
    renderPreview(monitor);

    selectedSignature = monitorSignature(monitor);
    highlightSelected();

    if (!silent) el.monitorCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function closeMonitor() {
    selectedId = null;
    selectedSignature = '';
    if (previewTimer) {
      clearInterval(previewTimer);
      previewTimer = null;
    }
    el.monitorCard.hidden = true;
    highlightSelected();
  }

  function highlightSelected() {
    const nodes = el.ring.children;
    for (let i = 0; i < nodes.length; i += 1) {
      nodes[i].classList.toggle('selected', nodes[i].dataset.id === selectedId);
    }
  }

  function updateUrlRow() {
    const feed = feedById(el.monitorFeed.value);
    el.monitorUrlRow.hidden = !(feed && (feed.options || []).indexOf('url') !== -1);
    el.cameraFinder.hidden = !(feed && feed.kind === 'camera');
    if (el.cameraFinder.hidden) el.cameraList.innerHTML = '';
  }

  function addOpenLink(url) {
    if (!url) return;
    const link = document.createElement('a');
    link.className = 'nav-link';
    link.href = url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = 'Open source';
    link.style.display = 'inline-block';
    link.style.marginTop = '8px';
    el.monitorMeta.appendChild(link);
  }

  function renderPreview(monitor) {
    if (previewTimer) {
      clearInterval(previewTimer);
      previewTimer = null;
    }
    el.monitorPreview.innerHTML = '';
    el.monitorMeta.innerHTML = '';

    const kind = feedKind(monitor.feed);
    if (!monitor.feed) {
      el.monitorPreview.textContent = 'No feed assigned. Pick one below.';
      return;
    }

    if (kind === 'stream') {
      const image = document.createElement('img');
      image.src = '/stream';
      image.alt = 'Live CamFlow stream';
      el.monitorPreview.appendChild(image);
      el.monitorMeta.textContent = 'Live MJPEG from the paired CamFlow phone.';
      return;
    }

    if (kind === 'web') {
      if (!monitor.url) {
        el.monitorPreview.textContent = 'Set a page URL below.';
        return;
      }
      const frame = document.createElement('iframe');
      frame.src = monitor.url;
      frame.title = monitor.label || 'Web feed';
      frame.referrerPolicy = 'no-referrer';
      el.monitorPreview.appendChild(frame);
      el.monitorMeta.textContent = 'If the page refuses to embed, open it directly.';
      addOpenLink(monitor.url);
      return;
    }

    if (kind === 'link') {
      const image = document.createElement('img');
      image.src = tileUrl(monitor.id, 1280);
      image.alt = monitor.label || 'NASA Live still';
      el.monitorPreview.appendChild(image);
      el.monitorMeta.textContent = 'Still frame from NASA EPIC; live video opens on NASA.gov.';
      addOpenLink(monitor.live_url || monitor.url || 'https://www.nasa.gov/live/');
      previewTimer = setInterval(() => { image.src = tileUrl(monitor.id, 1280); }, 20000);
      return;
    }

    if (kind === 'camera' && !monitor.url) {
      el.monitorPreview.textContent = 'Set the RTSP stream URL below, or use Find ONVIF cameras.';
      return;
    }

    const image = document.createElement('img');
    image.src = tileUrl(monitor.id, 1280);
    image.alt = monitor.label || 'Monitor feed';
    el.monitorPreview.appendChild(image);

    if (kind === 'camera') {
      el.monitorMeta.textContent = 'RTSP snapshot, refreshed every few seconds.';
      previewTimer = setInterval(() => { image.src = tileUrl(monitor.id, 1280); }, 3000);
    } else {
      const feed = feedById(monitor.feed);
      el.monitorMeta.textContent = feed ? feed.attribution : '';
      previewTimer = setInterval(() => { image.src = tileUrl(monitor.id, 1280); }, 6000);
    }
  }

  // ------------------------------------------------------------------ ARIA

  function appendBubble(kind, text) {
    const bubble = document.createElement('div');
    bubble.className = `bubble ${kind}`;
    const paragraph = document.createElement('p');
    paragraph.textContent = text;
    bubble.appendChild(paragraph);
    el.ariaLog.appendChild(bubble);
    el.ariaLog.scrollTop = el.ariaLog.scrollHeight;
    return bubble;
  }

  function appendMeta(text) {
    const meta = document.createElement('div');
    meta.className = 'bubble meta';
    meta.textContent = text;
    el.ariaLog.appendChild(meta);
    el.ariaLog.scrollTop = el.ariaLog.scrollHeight;
  }

  function renderDecision(decision) {
    const bubble = document.createElement('div');
    bubble.className = 'bubble aria';

    const chip = document.createElement('span');
    chip.className = 'decision';
    chip.dataset.decision = decision.decision;
    chip.textContent = decision.decision;
    bubble.appendChild(chip);

    const text = document.createElement('span');
    text.textContent = `${decision.action} · ${decision.reason}`;
    bubble.appendChild(text);

    el.ariaLog.appendChild(bubble);
    el.ariaLog.scrollTop = el.ariaLog.scrollHeight;
  }

  function renderProposal(proposal) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'proposal-btn';
    button.textContent = `Confirm: ${proposal.description}`;
    button.addEventListener('click', async () => {
      button.disabled = true;
      const result = await fetchJson('/api/dome/aria/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: proposal.id })
      });
      const data = result.json || {};
      appendBubble(data.ok ? 'aria' : 'aria error', data.reply || data.error || 'No answer.');
      button.remove();
      await load();
      loadAudit();
    });
    el.ariaLog.appendChild(button);
    el.ariaLog.scrollTop = el.ariaLog.scrollHeight;
  }

  async function sendAria(message) {
    appendBubble('user', message);
    const result = await fetchJson('/api/dome/aria', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: message, level: Number(el.ariaLevel.value) })
    });
    const data = result.json || {};
    if (data.ok === false) {
      appendBubble('aria error', data.error || 'The assistant could not answer.');
      return;
    }
    appendBubble('aria', data.reply || 'No answer.');
    if (data.provenance) appendMeta(`source: ${data.provenance}`);
    (data.decisions || []).forEach(renderDecision);
    (data.proposals || []).forEach(renderProposal);
    await load();
    loadAudit();
  }

  async function loadAudit() {
    const result = await fetchJson('/api/dome/aria/audit?n=15');
    if (!result.json || !result.json.ok) return;
    const items = result.json.data || [];
    el.auditList.innerHTML = '';
    if (!items.length) {
      const empty = document.createElement('li');
      empty.className = 'control-note';
      empty.textContent = 'No decisions yet.';
      el.auditList.appendChild(empty);
      return;
    }
    items.forEach((item) => {
      const row = document.createElement('li');
      row.dataset.decision = item.decision;

      const action = document.createElement('b');
      action.textContent = item.action;
      row.appendChild(action);
      row.appendChild(document.createTextNode(` · ${item.decision} · ${item.reason}`));

      const meta = document.createElement('div');
      meta.className = 'control-note';
      meta.textContent = `${item.ts} · L${item.level} · confidence ${item.confidence}`;
      row.appendChild(meta);

      el.auditList.appendChild(row);
    });
  }

  // --------------------------------------------------------------- events

  function bindEvents() {
    el.pauseBtn.addEventListener('click', () => {
      if (state) updateDome({ paused: !state.paused });
    });
    el.hubPause.addEventListener('click', () => {
      if (state) updateDome({ paused: !state.paused });
    });

    el.reverseBtn.addEventListener('click', () => {
      if (!state) return;
      const current = Number(state.speed_dps) || 0;
      updateDome({ speed_dps: current === 0 ? -3 : -current });
    });
    el.zeroBtn.addEventListener('click', () => updateDome({ speed_dps: 0 }));

    el.speedRange.addEventListener('input', () => {
      const value = Number(el.speedRange.value);
      el.speedValue.textContent = `${value.toFixed(1)}°/s`;
      speed = state && state.paused ? 0 : value;
    });
    el.speedRange.addEventListener('change', () => updateDome({ speed_dps: Number(el.speedRange.value) }));

    el.angleRange.addEventListener('input', () => {
      rotation = Number(el.angleRange.value);
      el.angleValue.textContent = `${Math.round(rotation)}°`;
      applyTransforms();
    });
    el.angleRange.addEventListener('change', () => updateDome({ rotation_deg: Number(el.angleRange.value) }));

    el.refractRange.addEventListener('input', () => {
      const value = Number(el.refractRange.value);
      el.refractValue.textContent = `${value}%`;
      el.stage.style.setProperty('--refract', String(value / 100));
    });
    el.refractRange.addEventListener('change', () => updateDome({ refraction: Number(el.refractRange.value) }));

    el.ariaForm.addEventListener('submit', (event) => {
      event.preventDefault();
      const message = el.ariaInput.value.trim();
      if (!message) return;
      el.ariaInput.value = '';
      sendAria(message);
    });

    el.ariaLevel.addEventListener('change', async () => {
      const level = Number(el.ariaLevel.value);
      const result = await fetchJson('/api/dome/aria/level', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ level: level })
      });
      if (result.json && result.json.ok) {
        el.ariaLevelBadge.textContent = LEVEL_LABELS[level];
        appendMeta(`Autonomy level set to ${level} · ${LEVEL_LABELS[level]}`);
      }
    });

    el.auditClear.addEventListener('click', async () => {
      await fetchJson('/api/dome/aria/audit', { method: 'DELETE' });
      loadAudit();
    });

    el.monitorClose.addEventListener('click', closeMonitor);
    el.monitorFeed.addEventListener('change', updateUrlRow);

    el.monitorDiscover.addEventListener('click', async () => {
      el.cameraList.innerHTML = '';
      const pending = document.createElement('li');
      pending.className = 'control-note';
      pending.textContent = 'Searching the LAN for ONVIF cameras…';
      el.cameraList.appendChild(pending);
      el.monitorDiscover.disabled = true;

      const result = await fetchJson('/api/dome/cameras/discover?timeout=2');
      el.monitorDiscover.disabled = false;
      const data = (result.json && result.json.ok && result.json.data) || {};
      const cameras = data.cameras || [];
      el.cameraList.innerHTML = '';

      if (!cameras.length) {
        const none = document.createElement('li');
        none.className = 'control-note';
        none.textContent = 'No camera answered. Enter the stream URL manually.';
        el.cameraList.appendChild(none);
        return;
      }

      cameras.forEach((camera) => {
        const item = document.createElement('li');
        const pick = document.createElement('button');
        pick.type = 'button';
        pick.textContent = `${camera.name} · ${camera.host || 'unknown host'}`;
        pick.addEventListener('click', () => {
          const hints = camera.rtsp_hints || [];
          const hint = hints[0] || (camera.host ? `rtsp://${camera.host}:554/` : '');
          if (hint) el.monitorUrl.value = hint;
          el.monitorMeta.textContent = hint
            ? 'URL filled from discovery. Add the camera path and credentials, then Apply.'
            : 'The camera answered but offered no RTSP address; enter the URL manually.';
        });
        item.appendChild(pick);
        el.cameraList.appendChild(item);
      });
    });

    el.monitorSave.addEventListener('click', async () => {
      if (!selectedId || !state) return;
      const result = await fetchJson(`/api/dome/monitors/${selectedId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          feed: el.monitorFeed.value,
          label: el.monitorLabel.value,
          url: el.monitorUrl.value
        })
      });
      const data = result.json || {};
      if (!data.ok) {
        el.monitorMeta.textContent = data.error || 'Could not save.';
        return;
      }
      state = data.data;
      render();
      openMonitor(selectedId, true);
      el.monitorMeta.textContent = 'Saved.';
    });

    el.monitorSnapshot.addEventListener('click', async () => {
      if (!selectedId) return;
      const result = await fetchJson(`/api/dome/monitors/${selectedId}/snapshot`, { method: 'POST' });
      const data = result.json || {};
      el.monitorMeta.textContent = data.ok ? `Saved to ${data.path}` : (data.error || 'Snapshot failed.');
    });

    el.monitorInspect.addEventListener('click', async () => {
      if (!selectedId) return;
      el.monitorMeta.textContent = 'Inspecting this monitor…';
      const result = await fetchJson(`/api/dome/monitors/${selectedId}/inspect`, { method: 'POST' });
      const data = result.json || {};
      if (data.ok && data.data) {
        el.monitorMeta.textContent = data.data.summary
          ? `AI: ${data.data.summary}`
          : 'AI inspection returned no summary.';
      } else {
        el.monitorMeta.textContent = data.error || 'Inspection failed.';
      }
    });

    el.monitorClear.addEventListener('click', async () => {
      if (!selectedId) return;
      const result = await fetchJson(`/api/dome/monitors/${selectedId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ feed: '', label: '', url: '' })
      });
      const data = result.json || {};
      if (data.ok) {
        state = data.data;
        render();
        openMonitor(selectedId, true);
      }
    });

    let resizeTimer = null;
    window.addEventListener('resize', () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => { if (state) buildRing(); }, 150);
    });
  }

  // ------------------------------------------------------------ background

  function animate(timestamp) {
    if (!lastFrameTs) lastFrameTs = timestamp;
    const delta = Math.min(0.1, (timestamp - lastFrameTs) / 1000);
    lastFrameTs = timestamp;

    if (!reduceMotion && speed !== 0 && !document.hidden) {
      rotation = (rotation + speed * delta) % 360;
      if (rotation < 0) rotation += 360;
      applyTransforms();
    }
    requestAnimationFrame(animate);
  }

  function refreshTiles() {
    if (!state || document.hidden) return;
    const nodes = el.ring.children;
    for (let i = 0; i < nodes.length; i += 1) {
      const node = nodes[i];
      setTimeout(() => {
        const image = node.querySelector('img.monitor-tile');
        if (image) image.src = tileUrl(node.dataset.id, 320);
      }, i * 120);
    }
  }

  async function poll() {
    if (syncing || document.hidden) return;
    const active = document.activeElement ? document.activeElement.id : '';
    if (['speedRange', 'angleRange', 'refractRange', 'monitorFeed', 'monitorLabel',
         'monitorUrl', 'ariaInput'].indexOf(active) !== -1) {
      return;
    }
    const result = await fetchJson('/api/dome');
    if (result.json && result.json.ok) applyData(result.json.data, false);
  }

  // ------------------------------------------------------------------ boot

  document.addEventListener('DOMContentLoaded', () => {
    bindEvents();
    load().then(loadAudit);
    setInterval(poll, 9000);
    setInterval(refreshTiles, 7000);
    requestAnimationFrame(animate);
  });
})();
