const state = {
  lastSourcesJSON: null,
  lastSourcesData: null,
  browsePath: null,
  browseDefaultName: '',
  browseMode: 'backup',
  activeTab: localStorage.getItem('saveteam-active-tab') || null,
  groupColors: {},
  pollTimers: [],
  searchQuery: '',
  autoTargetsByPath: {},
  keepSnapshots: null,
  backupsRoot: null,
};

function updateBackupsHint() {
  const hintEl = document.getElementById('backups-hint');
  if (!hintEl) return;
  const n = state.keepSnapshots;
  const nText = n ? `les ${n} dernières` : 'les plus récentes';
  hintEl.textContent =
    `Chaque jeu garde automatiquement ${nText} sauvegardes — les plus vieilles sont supprimées toutes seules pour ne pas remplir le disque. ` +
    `Pour qu'une sauvegarde précise ne soit jamais supprimée, clique sur « Conserver ».`;
}

const STALE_BACKUP_DAYS = 3;

const GROUP_COLOR_PALETTE = ['#0071e3', '#8b5cf6', '#10b981', '#f59e0b', '#ec4899', '#06b6d4', '#ef4444', '#84cc16'];

function colorForGroup(groupId) {
  if (!(groupId in state.groupColors)) {
    const idx = Object.keys(state.groupColors).length % GROUP_COLOR_PALETTE.length;
    state.groupColors[groupId] = GROUP_COLOR_PALETTE[idx];
  }
  return state.groupColors[groupId];
}

function fmtTime(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString();
}
function fmtBytes(bytes) {
  if (bytes == null) return '—';
  const gb = bytes / 1e9;
  return gb >= 1 ? `${gb.toFixed(1)} Go` : `${(bytes / 1e6).toFixed(0)} Mo`;
}
function fmtSeconds(s) {
  const m = Math.floor(s / 60);
  const r = s % 60;
  return r ? `${m}m ${r}s` : `${m}m`;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

async function jget(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
  return r.json();
}
async function jpost(url, body) {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
  return r.json();
}
async function jdel(url) {
  const r = await fetch(url, { method: 'DELETE' });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
  return r.json();
}

let toastTimer = null;
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hidden'), 4500);
}

function placeholderImage() {
  return el('div', 'card-image placeholder', '🎮');
}

function cardImage(entry) {
  // Steam's own CDN, keyed by real Steam appid - only meaningful for actual
  // Steam games. Non-Steam shortcut IDs are locally-generated pseudo-appids
  // and won't match anything there, so we don't even try for those.
  if (entry.kind !== 'steam') return placeholderImage();
  const img = el('img', 'card-image');
  img.loading = 'lazy';
  img.alt = '';
  img.src = `https://cdn.akamai.steamstatic.com/steam/apps/${entry.id}/header.jpg`;
  // The simple URL above only works for older store listings. Newer ones use
  // a content-hashed CDN path we can't guess - fall back to asking our own
  // server to look it up via Steam's store API (cached there) before giving up.
  img.onerror = async () => {
    img.onerror = null;
    try {
      const data = await jget('/api/cover?appid=' + encodeURIComponent(entry.id));
      if (data.header_image) {
        img.src = data.header_image;
        img.onerror = () => img.replaceWith(placeholderImage());
        return;
      }
    } catch (e) { /* fall through to placeholder */ }
    img.replaceWith(placeholderImage());
  };
  return img;
}

function backupStalenessBadge(entry) {
  const target = state.autoTargetsByPath[entry.path];
  if (!target || !target.enabled) return '';
  if (!target.last_backup_at) return '<span class="badge never">jamais sauvegardé</span>';
  const days = (Date.now() / 1000 - target.last_backup_at) / 86400;
  if (days > STALE_BACKUP_DAYS) return `<span class="badge stale">sauvegardé il y a ${Math.floor(days)}j</span>`;
  return '';
}

function badgeFor(entry) {
  const parts = [];
  if (entry.kind === 'tool') parts.push('<span class="badge tool">outil</span>');
  if (entry.kind === 'unknown') parts.push('<span class="badge unknown">non résolu</span>');
  if (entry.kind === 'nonsteam') parts.push('<span class="badge nonsteam">non-Steam</span>');
  if (entry.kind === 'steam') parts.push('<span class="badge steamcloud">Steam Cloud</span>');
  if (entry.not_launched) parts.push('<span class="badge">jamais lancé</span>');
  if (entry.is_symlink) parts.push('<span class="badge symlink">déplacé (actif)</span>');
  if (entry.kind === 'nonsteam' && entry.tracked_count > 0) {
    parts.push(`<span class="badge ok">✓ sauvegarde auto (${entry.tracked_count})</span>`);
  }
  if (entry.kind === 'nonsteam' && entry.exe_outside_prefix === true) {
    parts.push('<span class="badge ok" title="Son .exe vit en dehors du préfixe — réinstaller/réinitialiser ce préfixe ne touchera pas à l\'appli ni à ses jeux">app protégée</span>');
  } else if (entry.kind === 'nonsteam' && entry.exe_outside_prefix === false) {
    parts.push('<span class="badge stale" title="Son .exe vit dans ce préfixe — le réinstaller/réinitialiser effacerait l\'appli et ses jeux">app dans le préfixe</span>');
  }
  const staleBadge = backupStalenessBadge(entry);
  if (staleBadge) parts.push(staleBadge);
  return parts.join(' ');
}

function setActiveTab(tab) {
  state.activeTab = tab;
  localStorage.setItem('saveteam-active-tab', tab);
  document.querySelectorAll('.tab-btn').forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('[data-tab-panel]').forEach((p) => p.classList.toggle('tab-visible', p.dataset.tabPanel === tab));
  document.querySelectorAll('.group[data-group]').forEach((g) => g.classList.toggle('tab-visible', g.dataset.group === tab));
}

document.querySelectorAll('.tabs .tab-btn').forEach((btn) => {
  btn.onclick = () => setActiveTab(btn.dataset.tab);
});

function syncToolbarHeight() {
  const toolbar = document.querySelector('.toolbar');
  if (toolbar) document.documentElement.style.setProperty('--toolbar-h', toolbar.offsetHeight + 'px');
}
window.addEventListener('resize', syncToolbarHeight);
syncToolbarHeight();

function renderGroupTabs(groups) {
  const container = document.getElementById('group-tabs');
  const existingIds = new Set(Array.from(container.querySelectorAll('.tab-btn')).map((b) => b.dataset.tab));
  const currentIds = new Set(groups.map((g) => g.id));
  // drop tabs for groups that no longer exist (e.g. removed in Settings)
  for (const id of existingIds) {
    if (!currentIds.has(id)) container.querySelector(`.tab-btn[data-tab="${id}"]`)?.remove();
  }
  for (const g of groups) {
    let btn = container.querySelector(`.tab-btn[data-tab="${g.id}"]`);
    if (!btn) {
      btn = el('button', 'tab-btn');
      btn.dataset.tab = g.id;
      btn.style.setProperty('--tab-color', colorForGroup(g.id));
      btn.appendChild(el('span', 'tab-dot'));
      btn.appendChild(document.createTextNode(''));
      btn.appendChild(el('span', 'tab-count'));
      btn.onclick = () => setActiveTab(g.id);
      container.appendChild(btn);
    }
    btn.childNodes[1].textContent = g.label;
    btn.querySelector('.tab-count').textContent = g.mounted ? g.entries.length : '';
    const dot = btn.querySelector('.tab-dot');
    dot.classList.toggle('unmounted', !g.mounted);
    dot.title = g.mounted ? 'connecté' : 'non connecté';
  }
  // if the active tab vanished (or nothing picked yet), fall back to the first available one
  if (!document.querySelector(`.tab-btn[data-tab="${state.activeTab}"]`)) {
    const first = document.querySelector('.tabs .tab-btn');
    if (first) setActiveTab(first.dataset.tab);
  } else {
    setActiveTab(state.activeTab); // re-apply active/visible classes to freshly (re)built elements
  }
}

function renderCompatGroupsSettings(groups) {
  const list = document.getElementById('compat-groups-list');
  list.innerHTML = '';
  for (const g of groups) {
    const row = el('div', 'row');
    const main = el('div', 'row-main');
    const titleEl = el('div', 'row-title', g.label);
    if (!g.mounted) titleEl.innerHTML += ' <span class="badge-unmounted">non connecté</span>';
    main.appendChild(titleEl);
    main.appendChild(el('div', 'row-sub', g.path));
    row.appendChild(main);
    const actions = el('div', 'row-actions');
    const editBtn = el('button', 'btn ghost small', 'Modifier');
    editBtn.onclick = () => {
      document.getElementById('compat-group-id').value = g.id;
      document.getElementById('compat-group-label').value = g.label;
      document.getElementById('compat-group-path').value = g.path;
      document.getElementById('compat-group-submit-btn').textContent = 'Enregistrer les modifications';
      document.getElementById('compat-group-cancel-btn').style.display = '';
      document.getElementById('compat-group-label').scrollIntoView({ behavior: 'smooth', block: 'center' });
    };
    actions.appendChild(editBtn);
    const removeBtn = el('button', 'btn ghost small danger', 'Supprimer');
    removeBtn.disabled = groups.length <= 1;
    removeBtn.title = groups.length <= 1 ? 'Il doit rester au moins une source compatdata' : '';
    removeBtn.onclick = async () => {
      if (!confirm(`Arrêter le suivi de "${g.label}" ?\n\nCela oublie seulement l'emplacement source — aucune sauvegarde n'est supprimée.`)) return;
      try {
        await jdel('/api/compat-groups?id=' + encodeURIComponent(g.id));
        refreshSources(true);
      } catch (e) {
        alert('Impossible de supprimer : ' + e.message);
      }
    };
    actions.appendChild(removeBtn);
    row.appendChild(actions);
    list.appendChild(row);
  }
}

function renderGroups(data) {
  renderGroupTabs(data.groups);
  renderCompatGroupsSettings(data.groups);
  const container = document.getElementById('groups');
  container.innerHTML = '';
  for (const g of data.groups) {
    const wrap = el('div', 'group');
    wrap.dataset.group = g.id;
    wrap.classList.toggle('tab-visible', g.id === state.activeTab);
    wrap.style.setProperty('--group-color', colorForGroup(g.id));
    const head = el('div', 'group-head');
    head.appendChild(el('h2', null, g.label));
    if (!g.mounted) head.appendChild(el('span', 'badge-unmounted', 'non connecté / indisponible'));
    wrap.appendChild(head);
    const body = el('div', g.mounted && g.entries.length > 0 ? 'game-grid' : 'panel-body');
    if (g.mounted) {
      if (g.entries.length === 0) {
        body.appendChild(el('div', 'hint', 'Aucun jeu trouvé ici pour l\'instant.'));
      } else {
        for (const entry of g.entries) {
          const card = el('div', 'game-card');
          card.dataset.search = (entry.name || '').toLowerCase() + ' ' + String(entry.id).toLowerCase();
          card.appendChild(cardImage(entry));
          const badges = badgeFor(entry);
          if (badges) {
            const badgeRow = el('div', 'card-badges');
            badgeRow.innerHTML = badges;
            card.appendChild(badgeRow);
          }
          const titleEl = el('div', entry.name ? 'card-title' : 'card-title unresolved', entry.name || `Inconnu (id ${entry.id})`);
          card.appendChild(titleEl);
          const subEl = el('div', 'card-sub', entry.id);
          subEl.title = entry.path || '';
          card.appendChild(subEl);
          card.appendChild(el('div', 'card-meta', entry.not_launched ? 'Installé, jamais lancé' : fmtTime(entry.mtime)));
          const displayName = entry.name || `id-${entry.id}`;
          if (entry.not_launched) {
            card.appendChild(el('div', 'card-meta warn', 'Pas encore de dossier de sauvegarde — se lancera dès le premier lancement du jeu'));
          } else if (entry.kind === 'steam') {
            const btn = el('button', 'btn ghost small', 'Sauvegarder une copie manuelle');
            btn.title = 'Steam s\'en occupe déjà automatiquement — inutile dans la plupart des cas';
            btn.onclick = () => openBrowser(entry.path, displayName, 'backup');
            card.appendChild(btn);
          } else if (entry.kind === 'nonsteam') {
            if (entry.tracked_count > 0) {
              // fully automatic already - this is just an optional way to
              // double-check or add a second save location, not a required step
              const addBtn = el('button', 'btn ghost small', 'Vérifier / ajouter un dossier…');
              addBtn.onclick = () => openSaveDirPicker(entry, card);
              card.appendChild(addBtn);
            } else {
              card.appendChild(el('div', 'card-meta warn', 'Pas encore détecté automatiquement'));
              const pickBtn = el('button', 'btn ghost small', 'Configurer manuellement…');
              pickBtn.onclick = () => openSaveDirPicker(entry, card);
              card.appendChild(pickBtn);
            }
          } else {
            const btn = el('button', 'btn ghost small', 'Parcourir / Sauvegarder');
            btn.onclick = () => openBrowser(entry.path, displayName, 'backup');
            card.appendChild(btn);
          }
          body.appendChild(card);
        }
      }
    }
    wrap.appendChild(body);
    if (g.mounted && g.entries.length > 0) {
      const emptyHint = el('div', 'hint game-empty-hint', 'Aucun jeu ne correspond à la recherche.');
      emptyHint.hidden = true;
      wrap.appendChild(emptyHint);
    }
    container.appendChild(wrap);
  }
  applyGameFilter();

  const customList = document.getElementById('custom-list');
  customList.innerHTML = '';
  if (data.custom_sources.length === 0) {
    customList.appendChild(el('div', 'hint', 'Aucune source personnalisée pour l\'instant.'));
  }
  for (const cs of data.custom_sources) {
    const row = el('div', 'row');
    const main = el('div', 'row-main');
    const titleEl = el('div', 'row-title', cs.label);
    if (!cs.exists) titleEl.innerHTML += ' <span class="badge unknown">introuvable</span>';
    main.appendChild(titleEl);
    main.appendChild(el('div', 'row-sub', cs.path));
    row.appendChild(main);
    const actions = el('div', 'row-actions');
    const btn = el('button', 'btn ghost small', 'Parcourir / Sauvegarder');
    btn.onclick = () => openBrowser(cs.path, cs.label, 'backup');
    actions.appendChild(btn);
    const del = el('button', 'btn ghost small', 'Supprimer');
    del.onclick = async () => { await jdel('/api/custom-source?id=' + encodeURIComponent(cs.id)); refreshSources(true); };
    actions.appendChild(del);
    row.appendChild(actions);
    customList.appendChild(row);
  }
  const customTabCount = document.querySelector('.tab-btn[data-tab="custom"] .tab-count');
  if (customTabCount) customTabCount.textContent = data.custom_sources.length || '';
}

function applyGameFilter() {
  const query = state.searchQuery.trim().toLowerCase();
  for (const wrap of document.querySelectorAll('.group')) {
    let visibleCount = 0;
    const cards = wrap.querySelectorAll('.game-card');
    for (const card of cards) {
      const match = !query || (card.dataset.search || '').includes(query);
      card.hidden = !match;
      if (match) visibleCount += 1;
    }
    const emptyHint = wrap.querySelector('.game-empty-hint');
    if (emptyHint) emptyHint.hidden = !(query && cards.length > 0 && visibleCount === 0);
  }
}

document.getElementById('game-search').addEventListener('input', (ev) => {
  state.searchQuery = ev.target.value;
  applyGameFilter();
});

async function refreshSources(force) {
  try {
    const data = await jget('/api/sources' + (force ? '?force=1' : ''));
    const json = JSON.stringify(data);
    document.getElementById('status').textContent = '✅ à jour · vérifié à ' + new Date(data.scanned_at * 1000).toLocaleTimeString();
    if (json !== state.lastSourcesJSON) {
      state.lastSourcesJSON = json;
      state.lastSourcesData = data;
      renderGroups(data);
    }
  } catch (e) {
    document.getElementById('status').textContent = '⚠️ problème pendant la vérification : ' + e.message;
  }
}

async function openFolder(path, btn) {
  const original = btn ? btn.textContent : null;
  if (btn) { btn.disabled = true; btn.textContent = 'Ouverture…'; }
  try {
    await jpost('/api/open-folder', { path });
  } catch (e) {
    toast('Erreur : ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = original; }
  }
}

document.getElementById('open-backups-root-btn').onclick = (ev) => {
  if (state.backupsRoot) openFolder(state.backupsRoot, ev.currentTarget);
};

async function refreshBackups() {
  updateBackupsHint();
  const data = await jget('/api/backups');
  state.backupsRoot = data.root;
  const list = document.getElementById('backups-list');
  list.innerHTML = '';
  const backupsTabCount = document.querySelector('.tab-btn[data-tab="backups"] .tab-count');
  if (backupsTabCount) backupsTabCount.textContent = data.games.length || '';
  if (data.games.length === 0) {
    list.appendChild(el('div', 'hint', 'Aucune sauvegarde pour l\'instant.'));
    return;
  }
  for (const g of data.games) {
    const gwrap = el('div', 'backup-game');
    const header = el('div', 'header');
    header.appendChild(el('span', null, g.game));
    header.appendChild(el('span', 'hint', g.snapshots.length + ' sauvegarde(s)'));
    const openBtn = el('button', 'btn ghost small', '📁 Ouvrir');
    openBtn.title = 'Ouvrir ce dossier dans le gestionnaire de fichiers';
    openBtn.onclick = () => openFolder(g.path, openBtn);
    header.appendChild(openBtn);
    if (g.has_target) {
      const backupNowBtn = el('button', 'btn ghost small', 'Sauvegarder maintenant');
      backupNowBtn.title = 'Copier tout de suite le dossier du jeu vers une nouvelle sauvegarde ici';
      backupNowBtn.onclick = async () => {
        backupNowBtn.disabled = true;
        const original = backupNowBtn.textContent;
        backupNowBtn.textContent = 'Sauvegarde…';
        try {
          const result = await jpost('/api/backup-game', { game_name: g.game });
          const ok = result.results.filter((r) => r.status === 'backed_up').length;
          toast(ok > 0 ? `Sauvegardé : ${g.game}` : 'Rien à sauvegarder (dossier introuvable ou suivi en pause).');
          refreshBackups();
        } catch (e) {
          toast('Erreur : ' + e.message);
        } finally {
          backupNowBtn.disabled = false;
          backupNowBtn.textContent = original;
        }
      };
      header.appendChild(backupNowBtn);
    }
    gwrap.appendChild(header);
    for (const s of g.snapshots) {
      const srow = el('div', 'snap');
      const nameSpan = el('span', null, s.name);
      if (s.is_auto) nameSpan.innerHTML += ' <span class="badge">auto</span>';
      if (s.kept) nameSpan.innerHTML += ' <span class="badge ok">conservée</span>';
      srow.appendChild(nameSpan);
      srow.appendChild(el('span', 'path', s.path));
      if (s.is_auto) {
        const keepBtn = el('button', 'btn ghost small', 'Conserver');
        keepBtn.title = 'Exempter définitivement cette sauvegarde de la suppression automatique';
        keepBtn.onclick = async () => {
          await jpost('/api/backups/keep', { path: s.path });
          toast('Conservée : ' + s.name);
          refreshBackups();
        };
        srow.appendChild(keepBtn);
      } else {
        const unkeepBtn = el('button', 'btn ghost small', 'Remettre en rotation');
        unkeepBtn.title = 'Remet cette sauvegarde dans la rotation automatique (elle pourra de nouveau être supprimée automatiquement)';
        unkeepBtn.onclick = async () => {
          await jpost('/api/backups/unkeep', { path: s.path });
          toast('Remise en rotation : ' + s.name);
          refreshBackups();
        };
        srow.appendChild(unkeepBtn);
      }
      const restoreBtn = el('button', 'btn ghost small', 'Restaurer sur le jeu…');
      restoreBtn.title = 'Copier cette sauvegarde vers le dossier du jeu (ex : après réinstallation)';
      restoreBtn.onclick = () => {
        state.restoreSnapshotPath = s.path;
        openBrowser(s.source_path || null, g.game, 'restore');
      };
      srow.appendChild(restoreBtn);
      const deleteBtn = el('button', 'btn ghost small danger', 'Supprimer');
      deleteBtn.onclick = async () => {
        if (!confirm(`Supprimer définitivement cette sauvegarde ?\n\n${s.name}\n${s.path}\n\nAction irréversible.`)) return;
        await jdel('/api/backups?path=' + encodeURIComponent(s.path));
        toast('Supprimée : ' + s.name);
        refreshBackups();
      };
      srow.appendChild(deleteBtn);
      gwrap.appendChild(srow);
    }
    list.appendChild(gwrap);
  }
}

async function loadSettings() {
  const data = await jget('/api/settings');
  document.getElementById('settings-backup-root').value = data.backup_root;
  document.getElementById('settings-keep').value = data.keep_snapshots;
  document.getElementById('settings-interval').value = Math.round(data.backup_interval_seconds / 60);
  document.getElementById('settings-scan-interval').value = Math.round(data.scan_interval_seconds / 60);
  document.getElementById('settings-run-in-background').checked = data.run_in_background;
  document.getElementById('settings-keep').dataset.defaults = JSON.stringify(data.defaults);
  schedulePolling(data.scan_interval_seconds);
  state.keepSnapshots = data.keep_snapshots;
  updateBackupsHint();
  return data;
}

async function refreshDiskUsage() {
  try {
    const data = await jget('/api/disk-usage');
    document.getElementById('disk-usage').textContent =
      `${fmtBytes(data.backups_bytes)} de backups · ${fmtBytes(data.free_bytes)} libres`;
  } catch (e) { /* cosmetic - ignore */ }
}

function schedulePolling(scanIntervalSeconds) {
  state.pollTimers.forEach(clearInterval);
  const ms = Math.max(scanIntervalSeconds, 30) * 1000;
  state.pollTimers = [
    setInterval(() => refreshSources(false), ms),
    setInterval(refreshBackups, ms),
    setInterval(refreshAutoTargets, ms),
    setInterval(refreshDiskUsage, ms),
  ];
}

document.getElementById('settings-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const resultEl = document.getElementById('settings-result');
  resultEl.className = 'result-msg';
  const backupRoot = document.getElementById('settings-backup-root').value.trim();
  const keep = parseInt(document.getElementById('settings-keep').value, 10);
  const minutes = parseInt(document.getElementById('settings-interval').value, 10);
  const scanMinutes = parseInt(document.getElementById('settings-scan-interval').value, 10);
  const runInBackground = document.getElementById('settings-run-in-background').checked;
  try {
    await jpost('/api/settings', {
      backup_root: backupRoot,
      keep_snapshots: keep,
      backup_interval_seconds: minutes * 60,
      scan_interval_seconds: scanMinutes * 60,
      run_in_background: runInBackground,
    });
    resultEl.textContent = 'Enregistré. Appliqué tout de suite aux sauvegardes existantes ; le nouveau planning prend effet immédiatement.';
    schedulePolling(scanMinutes * 60);
    refreshAutoTargets();
    refreshDiskUsage();
  } catch (e) {
    resultEl.className = 'result-msg error';
    resultEl.textContent = 'Erreur : ' + e.message;
  }
});

document.getElementById('settings-reset-btn').onclick = async () => {
  const defaults = JSON.parse(document.getElementById('settings-keep').dataset.defaults || '{}');
  document.getElementById('settings-backup-root').value = defaults.backup_root;
  document.getElementById('settings-keep').value = defaults.keep_snapshots;
  document.getElementById('settings-interval').value = Math.round(defaults.backup_interval_seconds / 60);
  document.getElementById('settings-scan-interval').value = Math.round(defaults.scan_interval_seconds / 60);
  document.getElementById('settings-run-in-background').checked = defaults.run_in_background;
  document.getElementById('settings-form').requestSubmit();
};

document.getElementById('settings-backup-root-browse-btn').onclick = () => {
  openBrowser(document.getElementById('settings-backup-root').value || null, '', 'pick-backup-root');
};

async function loadAutostart() {
  const toggle = document.getElementById('settings-autostart');
  const hint = document.getElementById('autostart-hint');
  try {
    const data = await jget('/api/autostart');
    if (!data.supported) {
      toggle.disabled = true;
      hint.textContent = 'systemd n\'est pas disponible sur ce système — le démarrage automatique ne peut pas être géré ici';
      return;
    }
    toggle.checked = data.enabled;
  } catch (e) {
    hint.textContent = 'Erreur lors de la vérification du démarrage automatique : ' + e.message;
  }
}

document.getElementById('settings-autostart').addEventListener('change', async (ev) => {
  const checked = ev.target.checked;
  ev.target.disabled = true;
  try {
    const data = await jpost('/api/autostart', { enabled: checked });
    toast(data.enabled ? 'Démarrage automatique activé — prend effet à la prochaine connexion' : 'Démarrage automatique désactivé');
  } catch (e) {
    ev.target.checked = !checked; // revert on failure
    toast('Erreur : ' + e.message);
  } finally {
    ev.target.disabled = false;
  }
});

function resetCompatGroupForm() {
  document.getElementById('compat-group-id').value = '';
  document.getElementById('compat-group-label').value = '';
  document.getElementById('compat-group-path').value = '';
  document.getElementById('compat-group-submit-btn').textContent = 'Ajouter le groupe';
  document.getElementById('compat-group-cancel-btn').style.display = 'none';
}

document.getElementById('compat-group-cancel-btn').onclick = resetCompatGroupForm;

document.getElementById('compat-group-browse-btn').onclick = () => {
  openBrowser(document.getElementById('compat-group-path').value || null, '', 'pick-compat-group');
};

document.getElementById('compat-group-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const id = document.getElementById('compat-group-id').value.trim();
  const label = document.getElementById('compat-group-label').value.trim();
  const path = document.getElementById('compat-group-path').value.trim();
  if (!label || !path) return;
  try {
    await jpost('/api/compat-groups', { id, label, path });
    resetCompatGroupForm();
    refreshSources(true);
  } catch (e) {
    alert('Impossible d\'enregistrer : ' + e.message);
  }
});

async function addDiscoveredAsGroup(r, btn) {
  const guessedLabel = r.path.split('/').filter(Boolean).slice(-2, -1)[0] || 'Nouvelle source';
  const label = prompt('Nom pour cette source compatdata :', guessedLabel);
  if (!label) return;
  btn.disabled = true;
  try {
    await jpost('/api/compat-groups', { label, path: r.path });
    toast('Suivi activé : ' + label);
    refreshSources(true);
    document.getElementById('discover-btn').click();
  } catch (e) {
    alert('Impossible d\'ajouter : ' + e.message);
    btn.disabled = false;
  }
}

document.getElementById('discover-btn').onclick = async (ev) => {
  const btn = ev.currentTarget;
  const statusEl = document.getElementById('discover-status');
  const listEl = document.getElementById('discover-list');
  btn.disabled = true;
  statusEl.className = 'result-msg';
  statusEl.textContent = 'recherche sur les disques connectés…';
  listEl.innerHTML = '';
  try {
    const data = await jget('/api/discover-compatdata');
    statusEl.textContent = `${data.results.length} dossier(s) compatdata trouvé(s)`;
    for (const r of data.results) {
      const row = el('div', 'row');
      const main = el('div', 'row-main');
      const titleEl = el('div', 'row-title', r.path);
      titleEl.innerHTML += r.known
        ? ' <span class="badge ok">déjà utilisé</span>'
        : ' <span class="badge symlink">pas encore suivi</span>';
      main.appendChild(titleEl);
      if (r.path !== r.real_path) main.appendChild(el('div', 'row-sub', 'pointe vers : ' + r.real_path));
      row.appendChild(main);
      row.appendChild(el('div', 'row-meta', (r.entry_count ?? '?') + ' élément(s)'));
      if (!r.known) {
        const addBtn = el('button', 'btn primary small', '+ Suivre celui-ci');
        addBtn.onclick = () => addDiscoveredAsGroup(r, addBtn);
        row.appendChild(addBtn);
      }
      listEl.appendChild(row);
    }
  } catch (e) {
    statusEl.className = 'result-msg error';
    statusEl.textContent = 'Erreur : ' + e.message;
  } finally {
    btn.disabled = false;
  }
};

async function refreshAutoTargets(sync) {
  const data = await jget('/api/auto-targets' + (sync ? '?sync=1' : ''));
  state.autoTargetsByPath = {};
  for (const t of data.targets) state.autoTargetsByPath[t.source_path] = t;
  if (state.lastSourcesData) renderGroups(state.lastSourcesData); // refresh staleness badges
  state.keepSnapshots = data.keep;
  updateBackupsHint();
  document.getElementById('auto-backup-hint').textContent =
    `détection et sauvegarde automatiques toutes les ${fmtSeconds(data.interval_seconds)} · garde les ${data.keep} dernières sauvegardes auto par jeu · ignore ce qui n'a pas changé · jeux Steam non suivis (Steam Cloud s'en charge)`;
  const list = document.getElementById('auto-backup-list');
  list.innerHTML = '';
  const autoTabCount = document.querySelector('.tab-btn[data-tab="auto"] .tab-count');
  if (autoTabCount) autoTabCount.textContent = data.targets.length || '';
  if (data.targets.length === 0) {
    list.appendChild(el('div', 'hint', 'Aucun jeu détecté pour l\'instant sur les disques connectés.'));
    return;
  }
  for (const t of data.targets) {
    const row = el('div', 'row');
    const main = el('div', 'row-main');
    const titleEl = el('div', 'row-title', t.game_name);
    if (!t.enabled) titleEl.innerHTML += ' <span class="badge">en pause</span>';
    main.appendChild(titleEl);
    main.appendChild(el('div', 'row-sub', t.source_path));
    const statusText = t.last_backup_at ? 'dernière sauvegarde : ' + fmtTime(t.last_backup_at) : 'pas encore sauvegardé';
    main.appendChild(el('div', 'row-meta', statusText));
    row.appendChild(main);

    const actions = el('div', 'row-actions');
    const label = document.createElement('label');
    label.className = 'switch';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.checked = t.enabled;
    checkbox.onchange = async () => {
      await jpost('/api/auto-targets/toggle', { id: t.id, enabled: checkbox.checked });
      refreshAutoTargets();
    };
    label.appendChild(checkbox);
    label.appendChild(el('span', 'track'));
    actions.appendChild(label);
    const openBtn = el('button', 'btn ghost small', '📁 Ouvrir');
    openBtn.title = 'Ouvrir le dossier de sauvegarde du jeu (pour copier-coller à la main)';
    openBtn.onclick = () => openFolder(t.source_path, openBtn);
    actions.appendChild(openBtn);
    const delBtn = el('button', 'btn ghost small', 'Arrêter le suivi');
    delBtn.onclick = async () => {
      await jdel('/api/auto-targets?id=' + encodeURIComponent(t.id));
      refreshAutoTargets();
    };
    actions.appendChild(delBtn);
    row.appendChild(actions);
    list.appendChild(row);
  }
}

async function openSaveDirPicker(entry, card) {
  const existing = card.querySelector('.save-dir-picker');
  if (existing) {
    existing.remove();
    return;
  }
  const displayName = entry.name || `id-${entry.id}`;
  const panel = el('div', 'save-dir-picker');
  panel.appendChild(el('div', 'hint', 'Recherche des dossiers de sauvegarde…'));
  card.appendChild(panel);

  let url = '/api/suggest-save-dirs?path=' + encodeURIComponent(entry.path);
  if (entry.exe_path) url += '&exe=' + encodeURIComponent(entry.exe_path);
  let data;
  try {
    data = await jget(url);
  } catch (e) {
    panel.innerHTML = '';
    panel.appendChild(el('div', 'hint', 'Erreur : ' + e.message));
    return;
  }
  panel.innerHTML = '';

  const doBackup = async (path, resultEl) => {
    resultEl.textContent = 'copie en cours…';
    try {
      await jpost('/api/backup', { path, game_name: displayName });
      resultEl.textContent = 'Sauvegardé.';
      refreshSources(true);
      refreshBackups();
      refreshAutoTargets();
    } catch (e) {
      resultEl.textContent = 'Erreur : ' + e.message;
    }
  };

  if (data.candidates.length === 0) {
    panel.appendChild(el('div', 'hint', "Aucun dossier de sauvegarde détecté automatiquement — utilisez la recherche manuelle ci-dessous."));
  } else {
    for (const c of data.candidates) {
      const row = el('div', 'row' + (c.likely_junk ? ' dim' : ''));
      const main = el('div', 'row-main');
      main.appendChild(el('div', 'row-title', c.label));
      main.appendChild(el('div', 'row-sub', fmtBytes(c.size_bytes)));
      row.appendChild(main);
      const actions = el('div', 'row-actions');
      const btn = el('button', 'btn ghost small', 'Sauvegarder ce dossier');
      const resultEl = el('span', 'row-meta', '');
      btn.onclick = () => doBackup(c.path, resultEl);
      actions.appendChild(btn);
      actions.appendChild(resultEl);
      row.appendChild(actions);
      panel.appendChild(row);
    }
  }
  const browseBtn = el('button', 'btn ghost small', 'Parcourir manuellement…');
  browseBtn.onclick = () => openBrowser(data.steamuser_root, displayName, 'backup');
  panel.appendChild(browseBtn);
}

function openBrowser(path, defaultName, mode) {
  state.browseDefaultName = defaultName;
  state.browseMode = mode || 'backup';
  const backupBtn = document.getElementById('backup-btn');
  const nameInput = document.getElementById('backup-name');
  if (state.browseMode === 'pick' || state.browseMode === 'pick-compat-group' || state.browseMode === 'pick-backup-root') {
    nameInput.style.display = 'none';
    backupBtn.textContent = 'Utiliser ce dossier';
  } else if (state.browseMode === 'restore') {
    nameInput.style.display = 'none';
    backupBtn.textContent = 'Restaurer ici';
  } else {
    nameInput.style.display = '';
    nameInput.value = defaultName;
    backupBtn.textContent = 'Sauvegarder ce dossier';
  }
  document.getElementById('backup-result').textContent = '';
  document.getElementById('browser-modal').classList.remove('hidden');
  loadBrowsePath(path);
}

async function loadBrowsePath(path) {
  let data;
  try {
    data = await jget('/api/browse' + (path ? '?path=' + encodeURIComponent(path) : ''));
  } catch (e) {
    if (path) {
      // the starting path doesn't exist here (e.g. restoring a backup made
      // on another PC, or a game reinstalled under a new compatdata id) -
      // fall back to the root picker instead of leaving the modal stuck.
      toast("Cet emplacement n'existe pas sur cette machine — choisis le nouveau dossier.");
      return loadBrowsePath(null);
    }
    throw e;
  }
  state.browsePath = data.path;
  document.getElementById('browser-title').textContent = data.path || 'Choisis un point de départ';
  document.getElementById('browser-breadcrumbs').textContent = data.path || '';
  const entriesEl = document.getElementById('browser-entries');
  entriesEl.innerHTML = '';
  if (data.parent) {
    const up = el('div', 'entry dir', '.. (remonter)');
    up.onclick = () => loadBrowsePath(data.parent);
    entriesEl.appendChild(up);
  }
  // In restore mode, if we've landed on what looks like a Proton prefix
  // root (has a "pfx" subfolder), jump straight to suggesting the likely
  // save folder(s) inside it instead of making the user click through
  // pfx/drive_c/users/steamuser/AppData/Local/... by hand - this is exactly
  // the situation after reinstalling a game on a new PC (new compatdata id,
  // same save folder shape).
  if (state.browseMode === 'restore' && data.entries.some((e) => e.is_dir && e.name === 'pfx')) {
    try {
      const suggestions = await jget('/api/suggest-save-dirs?path=' + encodeURIComponent(data.path));
      const nonJunk = suggestions.candidates.filter((c) => !c.likely_junk);
      if (nonJunk.length > 0) {
        const section = el('div', 'hint', 'Emplacements probables :');
        entriesEl.appendChild(section);
        for (const c of nonJunk) {
          const row = el('div', 'entry dir suggested', `→ ${c.label} (${fmtBytes(c.size_bytes)})`);
          row.onclick = () => loadBrowsePath(c.path);
          entriesEl.appendChild(row);
        }
        entriesEl.appendChild(el('div', 'hint', 'Ou navigue manuellement :'));
      }
    } catch (e) { /* suggestions are a convenience, not required - ignore failures */ }
  }
  for (const e of data.entries) {
    const row = el('div', 'entry ' + (e.is_dir ? 'dir' : 'file'), e.name);
    if (e.is_dir) row.onclick = () => loadBrowsePath(e.path);
    entriesEl.appendChild(row);
  }
}

document.getElementById('browser-close').onclick = () => {
  document.getElementById('browser-modal').classList.add('hidden');
};

document.getElementById('backup-btn').onclick = async () => {
  if (!state.browsePath) return;
  if (state.browseMode === 'pick') {
    document.getElementById('custom-path').value = state.browsePath;
    document.getElementById('browser-modal').classList.add('hidden');
    return;
  }
  if (state.browseMode === 'pick-compat-group') {
    document.getElementById('compat-group-path').value = state.browsePath;
    document.getElementById('browser-modal').classList.add('hidden');
    return;
  }
  if (state.browseMode === 'pick-backup-root') {
    document.getElementById('settings-backup-root').value = state.browsePath;
    document.getElementById('browser-modal').classList.add('hidden');
    return;
  }
  const resultEl = document.getElementById('backup-result');
  resultEl.className = 'result-msg';

  if (state.browseMode === 'restore') {
    const ok = confirm(
      `Restaurer "${state.browseDefaultName}" dans :\n${state.browsePath}\n\n` +
      `Ceci écrase les fichiers correspondants à cet endroit. Le contenu actuel de ce dossier ` +
      `sera d'abord sauvegardé par sécurité s'il n'est pas vide, pour pouvoir annuler.`
    );
    if (!ok) return;
    resultEl.textContent = 'restauration en cours…';
    try {
      const r = await jpost('/api/restore', { snapshot_path: state.restoreSnapshotPath, dest_path: state.browsePath });
      resultEl.textContent = 'Restauré dans ' + r.restored_to + (r.safety_snapshot ? ' (l\'état précédent a été conservé comme sauvegarde)' : '');
      refreshBackups();
    } catch (e) {
      resultEl.className = 'result-msg error';
      resultEl.textContent = 'Erreur : ' + e.message;
    }
    return;
  }

  const name = document.getElementById('backup-name').value || state.browseDefaultName;
  resultEl.textContent = 'copie en cours…';
  try {
    const r = await jpost('/api/backup', { path: state.browsePath, game_name: name });
    resultEl.textContent = 'Sauvegardé dans ' + r.dest;
    refreshBackups();
    refreshAutoTargets();
  } catch (e) {
    resultEl.className = 'result-msg error';
    resultEl.textContent = 'Erreur : ' + e.message;
  }
};

document.getElementById('custom-browse-btn').onclick = () => {
  openBrowser(document.getElementById('custom-path').value || null, '', 'pick');
};

document.getElementById('custom-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const label = document.getElementById('custom-label').value.trim();
  const path = document.getElementById('custom-path').value.trim();
  if (!label || !path) return;
  try {
    await jpost('/api/custom-source', { label, path });
    document.getElementById('custom-label').value = '';
    document.getElementById('custom-path').value = '';
    refreshSources(true);
  } catch (e) {
    alert('Impossible d\'ajouter la source : ' + e.message);
  }
});

document.getElementById('backup-all-btn').onclick = async (ev) => {
  const btn = ev.currentTarget;
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = 'Sauvegarde en cours…';
  try {
    const data = await jpost('/api/backup-all', {});
    const backedUp = data.results.filter((r) => r.status === 'backed_up').length;
    const missing = data.results.filter((r) => r.status === 'missing').length;
    const errors = data.results.filter((r) => r.status === 'error').length;
    if (data.results.length === 0) {
      toast('Aucun jeu détecté sur les disques connectés pour l\'instant.');
    } else {
      let msg = `${backedUp} jeu(x) sauvegardé(s), conservé(s) définitivement.`;
      if (missing) msg += ` ${missing} source(s) introuvable(s).`;
      if (errors) msg += ` ${errors} erreur(s).`;
      toast(msg);
    }
    refreshBackups();
    refreshAutoTargets();
  } catch (e) {
    toast('Erreur : ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
};

document.getElementById('refresh-now-btn').onclick = () => {
  // the only thing that should force a real disk scan - page load and
  // periodic polling just reuse whatever's already cached server-side
  refreshSources(true);
  refreshBackups();
  refreshAutoTargets(true);
};

refreshSources(false);
refreshBackups();
refreshAutoTargets(false);
refreshDiskUsage();
loadAutostart();
loadSettings(); // also schedules recurring polling at the user's configured scan interval
