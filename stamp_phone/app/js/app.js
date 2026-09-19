import { db }   from './db.js';
import { sync } from './sync.js';

const API = window.location.origin;

// ============================================================
// Condition display helpers
// ============================================================
const CONDITION_ABBREV = {
  'Mint Never Hinged (MNH)': 'MNH',
  'Mint Hinged (MH)':        'MH',
  'Mint / Unused':           'Mint',
  'No Gum No Cancel':        'NGNC',
  'Fine Used (FU)':          'FU',
  'Used':                    'Used',
  'CTO (Cancelled to Order)':'CTO',
  'Faulty':                  'Faulty',
  'Other':                   'Other',
};

const CONDITION_CLASS = {
  'Mint Never Hinged (MNH)': 'cond-mnh',
  'Mint Hinged (MH)':        'cond-mh',
  'Mint / Unused':           'cond-mint',
  'No Gum No Cancel':        'cond-ngnc',
  'Fine Used (FU)':          'cond-fu',
  'Used':                    'cond-used',
  'CTO (Cancelled to Order)':'cond-cto',
  'Faulty':                  'cond-faulty',
  'Other':                   'cond-other',
};

function condAbbrev(cond) { return CONDITION_ABBREV[cond] ?? cond; }
function condClass(cond)  { return CONDITION_CLASS[cond]  ?? 'cond-other'; }

// ============================================================
// Sort label map
// ============================================================
const SORT_LABELS = {
  date:    'Newest first',
  oldest:  'Oldest first',
  title:   'Title A–Z',
  country: 'Country A–Z',
  scott:   'Scott #',
};

const FIELD_LABELS = {
  title:                'Title',
  scott_number:         'Scott #',
  country:              'Country',
  series:               'Series',
  issued_date:          'Issued',
  expired_date:         'Expired',
  face_value:           'Face value',
  print_run:            'Print run',
  size:                 'Size',
  perforation:          'Perforation',
  paper:                'Paper',
  gum:                  'Gum',
  printing:             'Printing',
  watermark:            'Watermark',
  colors:               'Colors',
  format:               'Format',
  emission:             'Emission',
  designers:            'Designers',
  description:          'Description',
  variants:             'Has variants',
  variant_set_id:       'Variant set',
  physical_location_id: 'Location',
  themes:               'Themes',
  copies:               'Copies',
};

function timeAgo(isoString) {
  const seconds = Math.floor((Date.now() - new Date(isoString).getTime()) / 1000);
  if (seconds < 60)  return 'Just now';
  const mins = Math.floor(seconds / 60);
  if (mins  < 60)    return `${mins}m ago`;
  const hrs  = Math.floor(mins  / 60);
  if (hrs   < 24)    return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

// ============================================================
// App state
// ============================================================
const state = {
  gallery: {
    stamps:  [],
    total:   0,
    offset:  0,
    scrollY: 0,
    loading: false,
    search:  '',
    country: '',
    theme:   '',
    sort:    'date',
  },
  filters:         null,
  filterDraft:     {},
  pendingStampIds: new Set(),
};

// ============================================================
// Utilities
// ============================================================
function $(id) { return document.getElementById(id); }

function el(tag, props = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if      (k === 'class') e.className = v;
    else if (k === 'html')  e.innerHTML = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  children.forEach(c => typeof c === 'string' ? e.append(c) : e.appendChild(c));
  return e;
}

let _toastTimer;
function showToast(msg, duration = 3000) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.remove('hidden');
  requestAnimationFrame(() => t.classList.add('visible'));
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => {
    t.classList.remove('visible');
    setTimeout(() => t.classList.add('hidden'), 200);
  }, duration);
}

// Toast now, and keep a copy. A toast is gone in three seconds, which is no use
// when an upload failed while you were looking at the stamp rather than the
// phone — the Sync page lists these afterwards.
function notify(level, msg, duration) {
  showToast(msg, duration);
  db.logEvent(level, msg).catch(() => { /* logging must never break the flow */ });
}

function imageUrl(filename) {
  return filename ? `${API}/api/images/${encodeURIComponent(filename)}` : null;
}

async function loadPendingIds() {
  try {
    const queue = await db.getEditQueue();
    state.pendingStampIds = new Set(queue.map(q => q.stamp_id));
  } catch {
    state.pendingStampIds = new Set();
  }
}

function escapeAttr(str) {
  return (str || '').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

const CHECK_SVG = `<svg class="sheet-option-check" viewBox="0 0 24 24" fill="currentColor">
  <path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/>
</svg>`;

// ============================================================
// Router
// ============================================================
function router() {
  closeEditSheet();
  const hash  = location.hash || '#/gallery';
  const parts = hash.replace('#/', '').split('/');
  const page  = parts[0] || 'gallery';
  const param = parts[1];

  document.querySelectorAll('.nav-item').forEach(el =>
    el.classList.toggle('active', el.dataset.page === page)
  );

  const backBtn = $('back-btn');
  $('header-actions').innerHTML = '';

  const pageToView = { gallery: 'gallery', '': 'gallery', stamp: 'detail', sync: 'sync' };
  const activeView = pageToView[page] ?? 'gallery';
  ['gallery', 'detail', 'sync'].forEach(v =>
    $(`view-${v}`).classList.toggle('hidden', v !== activeView)
  );

  switch (page) {
    case 'gallery':
    case '':
      backBtn.classList.add('hidden');
      $('page-title').textContent = 'Collection';
      if (!$('gallery-grid')) {
        renderGallery($('view-gallery'));
      } else {
        requestAnimationFrame(() => {
          $('view-gallery').scrollTop = state.gallery.scrollY;
        });
        updateFilterChips();
        updateFilterBtnState();
      }
      break;
    case 'stamp':
      backBtn.classList.remove('hidden');
      renderStampDetail($('view-detail'), parseInt(param, 10));
      break;
    case 'sync':
      backBtn.classList.add('hidden');
      $('page-title').textContent = 'Sync';
      renderSyncPage($('view-sync'));
      break;
    default:
      location.hash = '#/gallery';
  }
}

$('back-btn').addEventListener('click', () => history.back());
window.addEventListener('hashchange', router);

// ============================================================
// Filter sheet
// ============================================================
function openFilterSheet() {
  state.filterDraft = {
    country: state.gallery.country,
    theme:   state.gallery.theme,
    sort:    state.gallery.sort,
  };

  const body = $('filter-sheet-body');
  body.innerHTML = '';
  const filters = state.filters || { countries: [], themes: [] };

  // --- Sort chips ---
  body.appendChild(el('p', { class: 'sheet-section-label' }, 'Sort by'));
  const sortChips = el('div', { class: 'sheet-chips' });
  Object.entries(SORT_LABELS).forEach(([val, label]) => {
    const chip = el('button', {
      class: `chip${state.filterDraft.sort === val ? ' active' : ''}`,
      type: 'button',
    }, label);
    chip.addEventListener('click', () => {
      state.filterDraft.sort = val;
      sortChips.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
    });
    sortChips.appendChild(chip);
  });
  body.appendChild(sortChips);

  // --- Country list ---
  body.appendChild(el('p', { class: 'sheet-section-label' }, 'Country'));
  const countrySearch = el('input', {
    type: 'search', class: 'sheet-search', placeholder: 'Search countries…',
  });
  const countryList = el('div', { class: 'sheet-option-list' });

  function renderCountries(filter = '') {
    countryList.innerHTML = '';
    const allOpt = el('div', {
      class: `sheet-option${!state.filterDraft.country ? ' selected' : ''}`,
      html: `${CHECK_SVG}<span>All countries</span>`,
    });
    allOpt.addEventListener('click', () => {
      state.filterDraft.country = '';
      renderCountries(countrySearch.value);
    });
    countryList.appendChild(allOpt);

    filters.countries
      .filter(c => !filter || c.toLowerCase().includes(filter.toLowerCase()))
      .forEach(c => {
        const opt = el('div', {
          class: `sheet-option${state.filterDraft.country === c ? ' selected' : ''}`,
          html: `${CHECK_SVG}<span>${c}</span>`,
        });
        opt.addEventListener('click', () => {
          state.filterDraft.country = c;
          renderCountries(countrySearch.value);
        });
        countryList.appendChild(opt);
      });
  }

  countrySearch.addEventListener('input', e => renderCountries(e.target.value));
  renderCountries();
  body.append(countrySearch, countryList);

  // --- Theme list ---
  body.appendChild(el('p', { class: 'sheet-section-label' }, 'Theme'));
  const themeSearch = el('input', {
    type: 'search', class: 'sheet-search', placeholder: 'Search themes…',
  });
  const themeList = el('div', { class: 'sheet-option-list' });

  function renderThemes(filter = '') {
    themeList.innerHTML = '';
    const allOpt = el('div', {
      class: `sheet-option${!state.filterDraft.theme ? ' selected' : ''}`,
      html: `${CHECK_SVG}<span>All themes</span>`,
    });
    allOpt.addEventListener('click', () => {
      state.filterDraft.theme = '';
      renderThemes(themeSearch.value);
    });
    themeList.appendChild(allOpt);

    filters.themes
      .filter(t => !filter || t.toLowerCase().includes(filter.toLowerCase()))
      .forEach(t => {
        const opt = el('div', {
          class: `sheet-option${state.filterDraft.theme === t ? ' selected' : ''}`,
          html: `${CHECK_SVG}<span>${t}</span>`,
        });
        opt.addEventListener('click', () => {
          state.filterDraft.theme = t;
          renderThemes(themeSearch.value);
        });
        themeList.appendChild(opt);
      });
  }

  themeSearch.addEventListener('input', e => renderThemes(e.target.value));
  renderThemes();
  body.append(themeSearch, themeList);

  // Open
  const backdrop = $('filter-backdrop');
  const sheet    = $('filter-sheet');
  backdrop.classList.remove('hidden');
  sheet.classList.remove('hidden');
  requestAnimationFrame(() => {
    backdrop.classList.add('visible');
    sheet.classList.add('visible');
  });
}

function closeFilterSheet() {
  const backdrop = $('filter-backdrop');
  const sheet    = $('filter-sheet');
  backdrop.classList.remove('visible');
  sheet.classList.remove('visible');
  setTimeout(() => {
    backdrop.classList.add('hidden');
    sheet.classList.add('hidden');
  }, 300);
}

$('filter-backdrop').addEventListener('click', closeFilterSheet);

$('filter-apply-btn').addEventListener('click', () => {
  Object.assign(state.gallery, {
    country: state.filterDraft.country,
    theme:   state.filterDraft.theme,
    sort:    state.filterDraft.sort,
    offset:  0,
    stamps:  [],
  });
  closeFilterSheet();
  fetchStamps(true);
  updateFilterChips();
  updateFilterBtnState();
});

$('filter-clear-btn').addEventListener('click', () => {
  state.filterDraft = { country: '', theme: '', sort: 'date' };
  openFilterSheet();
});

function hasActiveFilters() {
  const g = state.gallery;
  return !!(g.country || g.theme || g.sort !== 'date');
}

function updateFilterBtnState() {
  const btn = document.querySelector('.filter-btn');
  if (btn) btn.classList.toggle('has-filters', hasActiveFilters());
}

function updateFilterChips() {
  const row = $('filter-chips');
  if (!row) return;
  row.innerHTML = '';

  // Sort chip (when not default)
  if (state.gallery.sort !== 'date') {
    const sc = el('button', { class: 'chip active', type: 'button' },
      SORT_LABELS[state.gallery.sort] + ' ×');
    sc.addEventListener('click', () => {
      state.gallery.sort   = 'date';
      state.gallery.offset = 0;
      state.gallery.stamps = [];
      fetchStamps(true);
      updateFilterChips();
      updateFilterBtnState();
    });
    row.appendChild(sc);
  }

  // Country chip
  if (state.gallery.country) {
    const cc = el('button', { class: 'chip active', type: 'button' },
      state.gallery.country + ' ×');
    cc.addEventListener('click', () => {
      state.gallery.country = '';
      state.gallery.offset  = 0;
      state.gallery.stamps  = [];
      fetchStamps(true);
      updateFilterChips();
      updateFilterBtnState();
    });
    row.appendChild(cc);
  }

  // Theme chip
  if (state.gallery.theme) {
    const tc = el('button', { class: 'chip active', type: 'button' },
      state.gallery.theme + ' ×');
    tc.addEventListener('click', () => {
      state.gallery.theme  = '';
      state.gallery.offset = 0;
      state.gallery.stamps = [];
      fetchStamps(true);
      updateFilterChips();
      updateFilterBtnState();
    });
    row.appendChild(tc);
  }
}

// ============================================================
// Gallery view
// ============================================================
// ============================================================
// Capture — photograph a stamp and send it to the laptop's incoming folder
// ============================================================

// When on, the camera reopens itself after each photo lands, so a run of stamps
// costs two taps each instead of three. Safari gates the file picker behind a
// user activation and returning from the native camera does not grant a fresh
// one, so iOS may well ignore the reopen — which is why this is a preference
// and not the default. If it is ignored the button still works normally.
let rapidCapture = false;

function reopenCamera() {
  const input = $('capture-input');
  if (input) input.click();
}

async function refreshCaptureBadge() {
  const badge = $('capture-badge');
  if (!badge) return;
  let n = 0;
  try { n = await db.countUploads(); } catch { /* older DB without the store */ }
  badge.textContent = n;
  badge.classList.toggle('hidden', n === 0);
}

function setupCapture() {
  const fab   = $('capture-fab');
  const input = $('capture-input');
  if (!fab || !input) return;

  fab.addEventListener('click', () => input.click());
  input.addEventListener('change', async () => {
    const file = input.files?.[0];
    // Clear it so photographing the same stamp twice still fires 'change'.
    input.value = '';
    if (file) await handleCapture(file);
  });

  refreshCaptureBadge();
  db.getPref('rapid_capture', false)
    .then(v => { rapidCapture = !!v; })
    .catch(() => { /* first run */ });
}

async function handleCapture(file) {
  const fab = $('capture-fab');
  if (fab) fab.disabled = true;
  showToast('Sending photo…', 30000);

  try {
    let result;
    try {
      result = await sync.uploadCapture(API, file);
    } catch {
      // Laptop unreachable. Hold the camera's original bytes and push them on
      // the next sync rather than losing the shot.
      await db.queueUpload(file);
      await refreshCaptureBadge();
      const n = await db.countUploads();
      notify('warn', `Laptop unreachable — ${n} photo${n !== 1 ? 's' : ''} waiting to upload`);
      if (rapidCapture) reopenCamera();
      return;
    }
    // The server answered. A rejection (wrong format, too large) would fail
    // the same way on retry, so report it instead of queueing it.
    if (result.ok) {
      showToast(rapidCapture ? 'Sent — reopening camera…' : 'Sent to laptop');
      db.logEvent('info', `Sent ${result.filename}`).catch(() => {});
      if (rapidCapture) reopenCamera();
    } else {
      notify('error', result.detail, 6000);
    }
  } catch {
    notify('error', 'Could not save the photo');
  } finally {
    if (fab) fab.disabled = false;
  }
}

async function renderGallery(container) {
  container.innerHTML = `
    <div class="gallery-toolbar">
      <div class="search-bar">
        <svg viewBox="0 0 24 24" fill="currentColor">
          <path d="M15.5 14h-.79l-.28-.27A6.47 6.47 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/>
        </svg>
        <input type="search" id="search-input" placeholder="Search stamps…" autocomplete="off"
               value="${escapeAttr(state.gallery.search)}">
      </div>
      <div class="filter-row">
        <div class="filter-chips" id="filter-chips"></div>
        <button class="filter-btn" id="filter-btn" type="button" title="Filter &amp; sort">
          <svg viewBox="0 0 24 24" fill="currentColor">
            <path d="M4.25 5.61C6.27 8.2 10 13 10 13v6c0 .55.45 1 1 1h2c.55 0 1-.45 1-1v-6s3.72-4.8 5.74-7.39A1 1 0 0 0 18.95 4H5.04a1 1 0 0 0-.79 1.61z"/>
          </svg>
        </button>
      </div>
    </div>
    <p class="gallery-count" id="gallery-count"></p>
    <div class="gallery-grid" id="gallery-grid"></div>
    <div class="gallery-footer" id="gallery-footer"></div>
  `;

  if (!state.filters) {
    try {
      const res = await fetch(`${API}/api/filters`);
      state.filters = await res.json();
    } catch {
      try {
        const [countries, themes] = await Promise.all([db.getCountries(), db.getThemes()]);
        state.filters = { countries, themes };
      } catch {
        showToast('Could not load filter options');
      }
    }
  }

  updateFilterChips();
  updateFilterBtnState();

  let searchTimer;
  $('search-input').addEventListener('input', e => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.gallery.search = e.target.value.trim();
      state.gallery.offset = 0;
      state.gallery.stamps = [];
      fetchStamps(true);
    }, 300);
  });

  $('filter-btn').addEventListener('click', openFilterSheet);

  fetchStamps(true);
}

function applyStampData(data, replace) {
  const grid   = $('gallery-grid');
  const footer = $('gallery-footer');

  if (replace) {
    state.gallery.stamps = data.stamps;
    grid.innerHTML = '';
  } else {
    state.gallery.stamps.push(...data.stamps);
  }

  state.gallery.total  = data.total;
  state.gallery.offset = state.gallery.stamps.length;

  const count = $('gallery-count');
  if (count) {
    if (state.gallery.total === 0) {
      count.textContent = 'No stamps found';
    } else {
      const stampTxt   = `${state.gallery.total.toLocaleString()} stamp${state.gallery.total !== 1 ? 's' : ''}`;
      const nCountries = state.filters?.countries?.length;
      const countryTxt = !state.gallery.country && nCountries
        ? ` · ${nCountries.toLocaleString()} countr${nCountries !== 1 ? 'ies' : 'y'}`
        : '';
      count.textContent = stampTxt + countryTxt;
    }
  }

  if (data.stamps.length === 0 && replace) {
    grid.appendChild(emptyState());
  } else {
    data.stamps.forEach(s => grid.appendChild(stampCard(s)));
  }

  if (footer) {
    footer.innerHTML = '';
    if (state.gallery.stamps.length < state.gallery.total) {
      const btn = el('button', { class: 'btn-tonal', type: 'button' }, 'Load more');
      btn.addEventListener('click', () => fetchStamps(false));
      footer.appendChild(btn);
    }
  }
}

async function fetchStamps(replace = false) {
  if (state.gallery.loading) return;
  state.gallery.loading = true;

  if (replace) await loadPendingIds();

  const grid   = $('gallery-grid');
  const footer = $('gallery-footer');
  if (!grid) { state.gallery.loading = false; return; }

  if (replace) {
    grid.innerHTML = skeletonCards(6);
    if (footer) footer.innerHTML = '';
  }

  const queryOffset = replace ? 0 : state.gallery.offset;

  try {
    const params = new URLSearchParams({
      limit: 50, offset: queryOffset, sort: state.gallery.sort,
    });
    if (state.gallery.search)  params.set('search',  state.gallery.search);
    if (state.gallery.country) params.set('country', state.gallery.country);
    if (state.gallery.theme)   params.set('theme',   state.gallery.theme);

    const res = await fetch(`${API}/api/stamps?${params}`);
    if (!res.ok) throw new Error(res.status);
    applyStampData(await res.json(), replace);
  } catch {
    try {
      const count = await db.countStamps();
      if (count === 0) {
        if (replace) grid.innerHTML = '';
        const galCount = $('gallery-count');
        if (galCount) galCount.textContent = '';
        grid.appendChild(el('div', {
          class: 'empty-state',
          style: 'grid-column:1/-1',
          html: `
            <svg viewBox="0 0 24 24" fill="currentColor">
              <path d="M12 4V1L8 5l4 4V6c3.31 0 6 2.69 6 6 0 1.01-.25 1.97-.7 2.8l1.46 1.46C19.54 15.03 20 13.57 20 12c0-4.42-3.58-8-8-8zm0 14c-3.31 0-6-2.69-6-6 0-1.01.25-1.97.7-2.8L5.24 7.74C4.46 8.97 4 10.43 4 12c0 4.42 3.58 8 8 8v3l4-4-4-4v3z"/>
            </svg>
            <h3>Not synced yet</h3>
            <p>Go to <strong>Sync</strong> to download your collection for offline use.</p>
          `,
        }));
      } else {
        const data = await db.getStamps({
          search: state.gallery.search,  country: state.gallery.country,
          theme:  state.gallery.theme,   sort:    state.gallery.sort,
          limit: 50, offset: queryOffset,
        });
        applyStampData(data, replace);
        showToast('Showing offline data');
      }
    } catch {
      if (replace) grid.innerHTML = '';
      grid.appendChild(errorState());
      showToast('Could not load stamps');
    }
  } finally {
    state.gallery.loading = false;
  }
}

// ============================================================
// Stamp card
// ============================================================
function stampCard(s) {
  const card = el('div', { class: 'stamp-card' });

  const imgUrl = imageUrl(s.image_filename);
  if (imgUrl) {
    const img = el('img', {
      class: 'stamp-card-image', src: imgUrl, alt: s.title, loading: 'lazy',
    });
    img.addEventListener('error', () => img.replaceWith(imagePlaceholder()));
    card.appendChild(img);
  } else {
    card.appendChild(imagePlaceholder());
  }

  const body = el('div', { class: 'stamp-card-body' });
  body.appendChild(el('p', { class: 'stamp-card-title' }, s.title));
  body.appendChild(el('p', { class: 'stamp-card-meta' }, `#${s.scott_number} · ${s.country}`));

  const badges = el('div', { class: 'stamp-card-badges' });
  if (s.conditions && s.conditions.length > 0) {
    s.conditions.forEach(cond => {
      badges.appendChild(el('span', { class: `badge ${condClass(cond)}` }, condAbbrev(cond)));
    });
  } else {
    badges.appendChild(el('span', { class: 'badge cond-none' }, 'No copies'));
  }
  body.appendChild(badges);

  card.appendChild(body);
  if (state.pendingStampIds.has(s.id)) {
    card.appendChild(el('div', { class: 'unsynced-overlay' }, 'Unsynced changes'));
  }
  card.addEventListener('click', () => {
    state.gallery.scrollY = $('view-gallery').scrollTop;
    location.hash = `#/stamp/${s.id}`;
  });
  return card;
}

function imagePlaceholder() {
  return el('div', { class: 'stamp-card-placeholder', html: `
    <svg viewBox="0 0 24 24" fill="currentColor">
      <path d="M21 19V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2zm-8.5-6.5l2.5 3.01L18 12l4 5H2l5-6 3.5 4.5z"/>
    </svg>
  `});
}

function skeletonCards(n) {
  return Array.from({ length: n }, () => `
    <div class="skeleton-card">
      <div class="skeleton skeleton-image"></div>
      <div class="skeleton-body">
        <div class="skeleton skeleton-line" style="width:85%"></div>
        <div class="skeleton skeleton-line" style="width:55%"></div>
      </div>
    </div>
  `).join('');
}

function emptyState() {
  return el('div', {
    class: 'empty-state',
    style: 'grid-column:1/-1',
    html: `
      <svg viewBox="0 0 24 24" fill="currentColor">
        <path d="M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 14H4V6h16v12z"/>
      </svg>
      <h3>No stamps found</h3>
      <p>Try adjusting your search or filters.</p>
    `,
  });
}

function errorState() {
  return el('div', {
    class: 'empty-state',
    style: 'grid-column:1/-1',
    html: `
      <svg viewBox="0 0 24 24" fill="currentColor">
        <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/>
      </svg>
      <h3>Could not load stamps</h3>
      <p>Make sure the server is running and you are connected to the same network.</p>
    `,
  });
}

// ============================================================
// Stamp detail view
// ============================================================

function formatDate(iso) {
  if (!iso) return null;
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      year: 'numeric', month: 'long', day: 'numeric',
    });
  } catch { return iso; }
}

function openLightbox(src, alt) {
  const overlay = el('div', { class: 'lightbox-overlay' });
  overlay.appendChild(el('img', { class: 'lightbox-img', src, alt: alt || '' }));
  overlay.addEventListener('click', () => {
    overlay.classList.remove('visible');
    setTimeout(() => overlay.remove(), 200);
  });
  document.body.appendChild(overlay);
  requestAnimationFrame(() => overlay.classList.add('visible'));
}

function buildDetailView(stamp) {
  const root = el('div', { class: 'detail-root' });

  // Images
  const images = stamp.images || [];
  const imageSection = el('div', { class: 'detail-image-section' });
  const noImgHtml = `<svg viewBox="0 0 24 24" fill="currentColor">
    <path d="M21 19V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2zm-8.5-6.5l2.5 3.01L18 12l4 5H2l5-6 3.5 4.5z"/>
  </svg>`;

  if (images.length > 0) {
    const mainImg = el('img', {
      class: 'detail-image-main',
      src: imageUrl(images[0]),
      alt: stamp.title,
      loading: 'eager',
    });
    mainImg.addEventListener('error', () =>
      mainImg.replaceWith(el('div', { class: 'detail-image-placeholder', html: noImgHtml }))
    );
    mainImg.addEventListener('click', () => openLightbox(mainImg.src, stamp.title));
    imageSection.appendChild(mainImg);

    if (images.length > 1) {
      const thumbRow = el('div', { class: 'detail-image-thumbs' });
      images.forEach((filename, i) => {
        const thumb = el('img', {
          class: `detail-image-thumb${i === 0 ? ' active' : ''}`,
          src: imageUrl(filename),
          alt: `Image ${i + 1}`,
          loading: 'lazy',
        });
        thumb.addEventListener('click', () => {
          mainImg.src = imageUrl(filename);
          thumbRow.querySelectorAll('.detail-image-thumb').forEach(t => t.classList.remove('active'));
          thumb.classList.add('active');
        });
        thumbRow.appendChild(thumb);
      });
      imageSection.appendChild(thumbRow);
    }
  } else {
    imageSection.appendChild(el('div', { class: 'detail-image-placeholder', html: noImgHtml }));
  }
  root.appendChild(imageSection);

  // Content
  const content = el('div', { class: 'detail-content' });

  // Identity: title, scott/country, themes
  const identity = el('div', { class: 'detail-identity' });
  identity.appendChild(el('h2', { class: 'detail-title' }, stamp.title));
  identity.appendChild(el('p', { class: 'detail-subtitle' }, `#${stamp.scott_number} · ${stamp.country}`));
  if (stamp.themes && stamp.themes.length > 0) {
    const themes = el('div', { class: 'detail-themes' });
    stamp.themes.forEach(t => themes.appendChild(el('span', { class: 'detail-theme-chip' }, t)));
    identity.appendChild(themes);
  }
  content.appendChild(identity);

  // Copies
  if (stamp.copies && stamp.copies.length > 0) {
    const sec = el('div', { class: 'detail-section' });
    sec.appendChild(el('p', { class: 'detail-section-title' }, 'In my collection'));
    const list = el('div', { class: 'copies-list' });
    stamp.copies.forEach(c => {
      const row = el('div', { class: 'copy-row' });
      row.appendChild(el('span', { class: `badge ${condClass(c.condition)}` }, condAbbrev(c.condition)));
      const info = el('div', { class: 'copy-info' });
      info.appendChild(el('p', { class: 'copy-condition' }, c.condition));
      const meta = [];
      if (c.quantity > 1) meta.push(`Qty: ${c.quantity}`);
      if (c.notes) meta.push(c.notes);
      if (meta.length) info.appendChild(el('p', { class: 'copy-meta' }, meta.join(' · ')));
      row.appendChild(info);
      list.appendChild(row);
    });
    sec.appendChild(list);
    content.appendChild(sec);
  }

  // Details grid
  const fields = [
    ['Series',      stamp.series_name || stamp.series],
    ['Issued',      formatDate(stamp.issued_date)],
    ['Expired',     formatDate(stamp.expired_date)],
    ['Face value',  stamp.face_value],
    ['Emission',    stamp.emission],
    ['Format',      stamp.format],
    ['Size',        stamp.size],
    ['Perforation', stamp.perforation],
    ['Paper',       stamp.paper],
    ['Gum',         stamp.gum],
    ['Printing',    stamp.printing],
    ['Colors',      stamp.colors],
    ['Watermark',   stamp.watermark],
    ['Designers',   stamp.designers],
    ['Print run',   stamp.print_run != null ? Number(stamp.print_run).toLocaleString() : null],
    ['Location',    stamp.physical_location_name],
    ['Variant set', stamp.variant_set_name],
  ].filter(([, v]) => v != null && v !== '');

  if (fields.length > 0) {
    const sec = el('div', { class: 'detail-section' });
    sec.appendChild(el('p', { class: 'detail-section-title' }, 'Details'));
    const grid = el('div', { class: 'fields-grid' });
    fields.forEach(([label, value]) => {
      grid.appendChild(el('span', { class: 'field-label' }, label));
      grid.appendChild(el('span', { class: 'field-value' }, String(value)));
    });
    sec.appendChild(grid);
    content.appendChild(sec);
  }

  // Description
  if (stamp.description) {
    const sec = el('div', { class: 'detail-section' });
    sec.appendChild(el('p', { class: 'detail-section-title' }, 'Description'));
    sec.appendChild(el('p', { class: 'detail-description' }, stamp.description));
    content.appendChild(sec);
  }

  root.appendChild(content);
  return root;
}

async function renderStampDetail(container, stampId) {
  $('page-title').textContent = 'Loading…';
  container.innerHTML = `
    <div style="display:flex;flex-direction:column">
      <div class="skeleton" style="width:100%;height:280px;border-radius:0;flex-shrink:0"></div>
      <div style="padding:16px;display:flex;flex-direction:column;gap:14px">
        <div class="skeleton skeleton-line" style="width:80%;height:20px"></div>
        <div class="skeleton skeleton-line" style="width:50%;height:14px"></div>
        <div class="skeleton skeleton-line" style="width:100%;height:72px;border-radius:var(--r-md)"></div>
        <div class="skeleton skeleton-line" style="width:65%"></div>
        <div class="skeleton skeleton-line" style="width:75%"></div>
        <div class="skeleton skeleton-line" style="width:55%"></div>
      </div>
    </div>
  `;

  let stamp   = null;
  let offline = false;

  try {
    const res = await fetch(`${API}/api/stamps/${stampId}`);
    if (!res.ok) throw new Error(res.status);
    stamp = await res.json();
  } catch {
    try {
      stamp = await db.getStamp(stampId);
      if (stamp) offline = true;
    } catch { /* IndexedDB unavailable */ }
  }

  if (!stamp) {
    container.innerHTML = '';
    container.appendChild(el('div', {
      class: 'placeholder-page',
      html: `
        <svg viewBox="0 0 24 24" fill="currentColor">
          <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/>
        </svg>
        <h2>Could not load stamp</h2>
        <p>Sync your collection first to browse offline.</p>
      `,
    }));
    showToast('Stamp not available — sync your collection first');
    return;
  }

  $('page-title').textContent = stamp.title;

  const editBtn = el('button', {
    class: 'icon-btn',
    title: 'Edit stamp',
    html: `<svg viewBox="0 0 24 24" fill="currentColor">
      <path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/>
    </svg>`,
  });
  editBtn.addEventListener('click', () => openEditSheet(stamp));
  $('header-actions').appendChild(editBtn);

  container.innerHTML = '';
  container.appendChild(buildDetailView(stamp));
  if (offline) showToast('Showing offline data');
}

// ============================================================
// Edit bottom sheet
// ============================================================

const CONDITIONS = Object.keys(CONDITION_ABBREV);

function closeEditSheet() {
  const saveBtn = $('edit-save-btn');
  if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = 'Save'; }
  const backdrop = $('edit-backdrop');
  const sheet    = $('edit-sheet');
  backdrop.classList.remove('visible');
  sheet.classList.remove('visible');
  setTimeout(() => {
    backdrop.classList.add('hidden');
    sheet.classList.add('hidden');
  }, 300);
}

function editFieldGroup(label, inputEl) {
  const g = el('div', { class: 'edit-field-group' });
  g.appendChild(el('label', { class: 'edit-field-label' }, label));
  g.appendChild(inputEl);
  return g;
}

function buildCopiesEditor(copies, conditions) {
  const list = el('div', { class: 'edit-copies-list' });

  function addCopyRow(copy) {
    const row = el('div', { class: 'edit-copy-row' });

    const condSel = el('select', { class: 'edit-select edit-copy-cond' });
    conditions.forEach(c => {
      const o = document.createElement('option');
      o.value = c;
      o.textContent = c;
      if (c === copy.condition) o.selected = true;
      condSel.appendChild(o);
    });

    const qtyInput = el('input', { type: 'number', class: 'edit-input edit-copy-qty', min: '1', max: '999' });
    qtyInput.value = copy.quantity || 1;

    const notesInput = el('input', { type: 'text', class: 'edit-input edit-copy-notes', placeholder: 'Notes' });
    notesInput.value = copy.notes || '';

    const delBtn = el('button', {
      class: 'icon-btn on-surface edit-copy-del', type: 'button', title: 'Remove',
      html: `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>`,
    });
    delBtn.addEventListener('click', () => row.remove());

    row.append(condSel, qtyInput, notesInput, delBtn);
    list.appendChild(row);
  }

  copies.forEach(c => addCopyRow(c));

  const addBtn = el('button', { class: 'btn-tonal edit-copies-add', type: 'button' }, '+ Add copy');
  addBtn.addEventListener('click', () => addCopyRow({ condition: conditions[0], quantity: 1, notes: '' }));

  const wrapper = el('div', { class: 'edit-copies-wrapper' });
  wrapper.append(list, addBtn);
  return wrapper;
}

function collectCopies(wrapper) {
  return [...wrapper.querySelectorAll('.edit-copy-row')].map(row => ({
    condition: row.querySelector('.edit-copy-cond').value,
    quantity:  parseInt(row.querySelector('.edit-copy-qty').value, 10)  || 1,
    notes:     row.querySelector('.edit-copy-notes').value.trim() || null,
  }));
}

function collectEditData(body, stamp) {
  const changes = {};
  const fv = id => document.getElementById(id)?.value?.trim() || null;
  const fi = id => { const v = document.getElementById(id)?.value; return v ? parseInt(v, 10) : null; };

  function diff(key, newVal, origVal) {
    const n = newVal  ?? null;
    const o = origVal ?? null;
    if (n !== o) changes[key] = n;
  }

  diff('title',                fv('edit-f-title'),        stamp.title);
  diff('scott_number',         fv('edit-f-scott'),        stamp.scott_number);
  diff('country',              fv('edit-f-country'),      stamp.country);
  diff('series',               fv('edit-f-series'),       stamp.series_name || stamp.series);
  diff('issued_date',          fv('edit-f-issued'),       stamp.issued_date?.split('T')[0]);
  diff('expired_date',         fv('edit-f-expired'),      stamp.expired_date?.split('T')[0]);
  diff('face_value',           fv('edit-f-face'),         stamp.face_value);
  diff('print_run',            fi('edit-f-print-run'),    stamp.print_run ?? null);
  diff('size',                 fv('edit-f-size'),         stamp.size);
  diff('perforation',          fv('edit-f-perforation'),  stamp.perforation);
  diff('paper',                fv('edit-f-paper'),        stamp.paper);
  diff('gum',                  fv('edit-f-gum'),          stamp.gum);
  diff('printing',             fv('edit-f-printing'),     stamp.printing);
  diff('watermark',            fv('edit-f-watermark'),    stamp.watermark);
  diff('colors',               fv('edit-f-colors'),       stamp.colors);
  diff('format',               fv('edit-f-format'),       stamp.format);
  diff('emission',             fv('edit-f-emission'),     stamp.emission);
  diff('designers',            fv('edit-f-designers'),    stamp.designers);
  diff('description',          document.getElementById('edit-f-description')?.value?.trim() || null,
                               stamp.description);
  diff('variants',             !!document.getElementById('edit-f-variants')?.checked,
                               !!stamp.variants);
  diff('variant_set_id',       fi('edit-f-variant-set'),  stamp.variant_set_id ?? null);
  diff('physical_location_id', fi('edit-f-location'),     stamp.physical_location_id ?? null);

  // Themes: compare sorted arrays
  const newThemes  = [...(body._selectedThemes ?? [])].sort();
  const origThemes = [...(stamp.themes || [])].sort();
  if (JSON.stringify(newThemes) !== JSON.stringify(origThemes)) {
    changes.themes = [...body._selectedThemes];
  }

  // Copies: compare without server-side IDs
  const newCopies  = collectCopies(document.getElementById('edit-copies-editor'));
  const origCopies = (stamp.copies || []).map(c => ({
    condition: c.condition,
    quantity:  c.quantity,
    notes:     c.notes ?? null,
  }));
  if (JSON.stringify(newCopies) !== JSON.stringify(origCopies)) {
    changes.copies = newCopies;
  }

  return changes;
}

async function saveEdit(stamp, body) {
  const saveBtn = $('edit-save-btn');
  saveBtn.disabled = true;
  saveBtn.textContent = 'Saving…';

  try {
    const changes = collectEditData(body, stamp);

    if (Object.keys(changes).length === 0) {
      closeEditSheet();
      showToast('No changes to save');
      return;
    }

    const abort  = new AbortController();
    const _timer = setTimeout(() => abort.abort(), 5000);
    try {
      const res = await fetch(`${API}/api/stamps/${stamp.id}`, {
        method:  'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(changes),
        signal:  abort.signal,
      });
      clearTimeout(_timer);
      if (!res.ok) throw new Error(res.status);
      const updated = await res.json();
      await db.saveAllStamps([updated]);
      closeEditSheet();
      showToast('Saved');
      renderStampDetail($('view-detail'), stamp.id);
    } catch {
      clearTimeout(_timer);
      await db.queueEdit(stamp.id, changes);
      await db.applyEditLocally(stamp.id, changes);
      closeEditSheet();
      showToast('Saved offline — will sync when connected');
      renderStampDetail($('view-detail'), stamp.id);
    }
  } catch {
    showToast('Save failed — please try again');
    saveBtn.disabled = false;
    saveBtn.textContent = 'Save';
  }
}

async function openEditSheet(stamp) {
  // Reference data for dropdowns — prefer live filters, fall back to IDB ref data
  let allThemes   = state.filters?.themes             ?? [];
  let variantSets = state.filters?.variant_sets       ?? [];
  let locations   = state.filters?.physical_locations ?? [];

  if (!allThemes.length || !variantSets.length) {
    try {
      const ref = await db.getRefData();
      if (ref) {
        if (!allThemes.length)   allThemes   = ref.themes             ?? [];
        if (!variantSets.length) variantSets = ref.variant_sets       ?? [];
        if (!locations.length)   locations   = ref.physical_locations ?? [];
      }
    } catch { /* use whatever we have */ }
  }

  const body = $('edit-sheet-body');
  body.innerHTML = '';
  body._selectedThemes = new Set(stamp.themes || []);

  // ---- Identification ----
  const sec1 = el('div', { class: 'edit-section' });
  sec1.appendChild(el('p', { class: 'edit-section-title' }, 'Identification'));
  const titleInput = el('input', { type: 'text', class: 'edit-input', id: 'edit-f-title' });
  titleInput.value = stamp.title || '';
  const scottInput = el('input', { type: 'text', class: 'edit-input', id: 'edit-f-scott' });
  scottInput.value = stamp.scott_number || '';
  const countryInput = el('input', { type: 'text', class: 'edit-input', id: 'edit-f-country' });
  countryInput.value = stamp.country || '';
  sec1.append(editFieldGroup('Title', titleInput), editFieldGroup('Scott #', scottInput), editFieldGroup('Country', countryInput));
  body.appendChild(sec1);

  // ---- Dates & Value ----
  const sec2 = el('div', { class: 'edit-section' });
  sec2.appendChild(el('p', { class: 'edit-section-title' }, 'Dates & Value'));
  const issuedInput = el('input', { type: 'date', class: 'edit-input', id: 'edit-f-issued' });
  issuedInput.value = stamp.issued_date?.split('T')[0] ?? '';
  const expiredInput = el('input', { type: 'date', class: 'edit-input', id: 'edit-f-expired' });
  expiredInput.value = stamp.expired_date?.split('T')[0] ?? '';
  const faceInput = el('input', { type: 'text', class: 'edit-input', id: 'edit-f-face' });
  faceInput.value = stamp.face_value || '';
  const printRunInput = el('input', { type: 'number', class: 'edit-input', id: 'edit-f-print-run', min: '1' });
  printRunInput.value = stamp.print_run ?? '';
  sec2.append(
    editFieldGroup('Issued', issuedInput),
    editFieldGroup('Expired', expiredInput),
    editFieldGroup('Face value', faceInput),
    editFieldGroup('Print run', printRunInput),
  );
  body.appendChild(sec2);

  // ---- Physical Details ----
  const sec3 = el('div', { class: 'edit-section' });
  sec3.appendChild(el('p', { class: 'edit-section-title' }, 'Physical Details'));
  [
    ['Size',        'size',        stamp.size],
    ['Perforation', 'perforation', stamp.perforation],
    ['Paper',       'paper',       stamp.paper],
    ['Gum',         'gum',         stamp.gum],
    ['Printing',    'printing',    stamp.printing],
    ['Watermark',   'watermark',   stamp.watermark],
    ['Colors',      'colors',      stamp.colors],
    ['Format',      'format',      stamp.format],
    ['Emission',    'emission',    stamp.emission],
    ['Designers',   'designers',   stamp.designers],
  ].forEach(([label, id, val]) => {
    const inp = el('input', { type: 'text', class: 'edit-input', id: `edit-f-${id}` });
    inp.value = val || '';
    sec3.appendChild(editFieldGroup(label, inp));
  });
  body.appendChild(sec3);

  // ---- Classification ----
  const sec4 = el('div', { class: 'edit-section' });
  sec4.appendChild(el('p', { class: 'edit-section-title' }, 'Classification'));
  const seriesInput = el('input', { type: 'text', class: 'edit-input', id: 'edit-f-series' });
  seriesInput.value = stamp.series_name || stamp.series || '';
  sec4.appendChild(editFieldGroup('Series', seriesInput));

  if (variantSets.length > 0) {
    const vsSel = el('select', { class: 'edit-select', id: 'edit-f-variant-set' });
    vsSel.appendChild(Object.assign(document.createElement('option'), { value: '', textContent: '— none —' }));
    variantSets.forEach(vs => {
      const o = Object.assign(document.createElement('option'), { value: String(vs.id), textContent: vs.name });
      if (vs.id === stamp.variant_set_id) o.selected = true;
      vsSel.appendChild(o);
    });
    sec4.appendChild(editFieldGroup('Variant set', vsSel));
  }

  if (locations.length > 0) {
    const locSel = el('select', { class: 'edit-select', id: 'edit-f-location' });
    locSel.appendChild(Object.assign(document.createElement('option'), { value: '', textContent: '— none —' }));
    locations.forEach(loc => {
      const o = Object.assign(document.createElement('option'), { value: String(loc.id), textContent: loc.name });
      if (loc.id === stamp.physical_location_id) o.selected = true;
      locSel.appendChild(o);
    });
    sec4.appendChild(editFieldGroup('Location', locSel));
  }

  const varRow = el('div', { class: 'edit-toggle-row' });
  const varCb  = el('input', { type: 'checkbox', class: 'edit-toggle', id: 'edit-f-variants' });
  varCb.checked = !!stamp.variants;
  varRow.append(varCb, el('label', { class: 'edit-toggle-label', for: 'edit-f-variants' }, 'Has variants'));
  sec4.appendChild(varRow);
  body.appendChild(sec4);

  // ---- Themes ----
  if (allThemes.length > 0) {
    const sec5 = el('div', { class: 'edit-section' });
    sec5.appendChild(el('p', { class: 'edit-section-title' }, 'Themes'));
    const searchInp = el('input', { type: 'search', class: 'edit-theme-search', placeholder: 'Search themes…' });
    const themeList = el('div', { class: 'edit-themes-list' });
    const selected  = body._selectedThemes;

    function renderThemes(filter = '') {
      themeList.innerHTML = '';
      allThemes
        .filter(t => !filter || t.toLowerCase().includes(filter.toLowerCase()))
        .forEach(theme => {
          const safeId = `et-${theme.replace(/[^a-z0-9]/gi, '-')}`;
          const row    = el('div', { class: 'edit-theme-row' });
          const cb     = el('input', { type: 'checkbox', class: 'edit-theme-cb', id: safeId });
          cb.checked   = selected.has(theme);
          cb.addEventListener('change', () => cb.checked ? selected.add(theme) : selected.delete(theme));
          row.append(cb, el('label', { class: 'edit-theme-label', for: safeId }, theme));
          themeList.appendChild(row);
        });
    }
    searchInp.addEventListener('input', e => renderThemes(e.target.value));
    renderThemes();
    sec5.append(searchInp, themeList);
    body.appendChild(sec5);
  }

  // ---- Description ----
  const sec6 = el('div', { class: 'edit-section' });
  sec6.appendChild(el('p', { class: 'edit-section-title' }, 'Description'));
  const descTa = el('textarea', { class: 'edit-textarea', id: 'edit-f-description', rows: '4' });
  descTa.value = stamp.description || '';
  sec6.appendChild(descTa);
  body.appendChild(sec6);

  // ---- Copies ----
  const sec7 = el('div', { class: 'edit-section' });
  sec7.appendChild(el('p', { class: 'edit-section-title' }, 'Copies'));
  const copiesEd = buildCopiesEditor(stamp.copies || [], CONDITIONS);
  copiesEd.id = 'edit-copies-editor';
  sec7.appendChild(copiesEd);
  body.appendChild(sec7);

  // Wire buttons
  $('edit-save-btn').onclick   = () => saveEdit(stamp, body);
  $('edit-cancel-btn').onclick = closeEditSheet;
  $('edit-backdrop').onclick   = closeEditSheet;

  // Open the sheet
  $('edit-backdrop').classList.remove('hidden');
  $('edit-sheet').classList.remove('hidden');
  requestAnimationFrame(() => {
    $('edit-backdrop').classList.add('visible');
    $('edit-sheet').classList.add('visible');
  });
}

// ============================================================
// Sync page
// ============================================================
async function renderSyncPage(container) {
  container.innerHTML = `<div class="sync-page">
    <div class="skeleton" style="height:120px;border-radius:var(--r-lg)"></div>
    <div class="skeleton skeleton-line" style="width:60%;margin-top:8px"></div>
  </div>`;

  let lastSync        = null;
  let stampCount      = 0;
  let queue           = [];
  let queueWithStamps = [];

  try {
    [lastSync, stampCount, queue] = await Promise.all([
      db.getLastSync(), db.countStamps(), db.getEditQueue(),
    ]);
  } catch { /* first run — no IDB data yet */ }

  if (queue.length > 0) {
    queueWithStamps = await Promise.all(
      queue.map(async item => {
        let stamp = null;
        try { stamp = await db.getStamp(item.stamp_id); } catch {}
        return { item, stamp };
      })
    );
  }

  const queueCount = queue.length;
  const page = el('div', { class: 'sync-page' });

  // --- Status card ---
  const card = el('div', { class: 'sync-status-card' });

  const syncRow = el('div', { class: 'sync-stat' });
  syncRow.appendChild(el('span', { class: 'sync-stat-label' }, 'Last synced'));
  syncRow.appendChild(el('span', { class: 'sync-stat-value' }, lastSync
    ? new Date(lastSync).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
    : 'Never'));
  card.appendChild(syncRow);

  const countRow = el('div', { class: 'sync-stat' });
  countRow.appendChild(el('span', { class: 'sync-stat-label' }, 'Local data'));
  countRow.appendChild(el('span', { class: 'sync-stat-value' },
    stampCount > 0 ? `${stampCount.toLocaleString()} stamps cached` : 'No local data — sync now'
  ));
  card.appendChild(countRow);

  const pendingStatRow = el('div', { class: 'sync-stat' });
  pendingStatRow.appendChild(el('span', { class: 'sync-stat-label' }, 'Pending edits'));
  pendingStatRow.appendChild(el('span', { class: 'sync-stat-value' },
    queueCount > 0 ? `${queueCount} edit${queueCount !== 1 ? 's' : ''} queued` : 'None'
  ));
  card.appendChild(pendingStatRow);
  page.appendChild(card);

  // --- Pending changes list ---
  if (queueWithStamps.length > 0) {
    const changesSection = el('div', { class: 'pending-section' });

    // Header with select-all
    const hdr = el('div', { class: 'pending-header' });
    hdr.appendChild(el('h3', { class: 'pending-title' }, `Pending Changes (${queueCount})`));
    const selectAllLbl = el('label', { class: 'pending-select-all' });
    const selectAllCb  = el('input', { type: 'checkbox' });
    selectAllCb.checked = true;
    selectAllCb.addEventListener('change', () => {
      document.querySelectorAll('.pending-cb').forEach(cb => { cb.checked = selectAllCb.checked; });
      updatePushBtn();
    });
    selectAllLbl.append(selectAllCb, ' Select all');
    hdr.appendChild(selectAllLbl);
    changesSection.appendChild(hdr);

    // Item list
    const list = el('div', { class: 'pending-list' });
    const noThumb = () => el('div', {
      class: 'pending-thumb pending-thumb-placeholder',
      html: `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M21 19V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2zm-8.5-6.5l2.5 3.01L18 12l4 5H2l5-6 3.5 4.5z"/></svg>`,
    });

    queueWithStamps.forEach(({ item, stamp }) => {
      const row = el('div', { class: 'pending-item' });

      const cb = el('input', { type: 'checkbox', class: 'pending-cb' });
      cb.checked = true;
      cb.dataset.queueId = String(item.id);
      cb.addEventListener('change', updatePushBtn);
      row.appendChild(cb);

      const thumbUrl = stamp?.images?.[0] ? imageUrl(stamp.images[0]) : null;
      if (thumbUrl) {
        const thumb = el('img', { class: 'pending-thumb', src: thumbUrl, alt: '', loading: 'lazy' });
        thumb.addEventListener('error', () => thumb.replaceWith(noThumb()));
        row.appendChild(thumb);
      } else {
        row.appendChild(noThumb());
      }

      const info = el('div', { class: 'pending-info' });
      info.appendChild(el('p', { class: 'pending-stamp-name' }, stamp?.title || `Stamp #${item.stamp_id}`));
      if (stamp?.scott_number) {
        info.appendChild(el('p', { class: 'pending-stamp-scott' }, `#${stamp.scott_number}`));
      }
      const changedFields = Object.keys(item.changes).map(k => FIELD_LABELS[k] || k).join(', ');
      if (changedFields) {
        info.appendChild(el('p', { class: 'pending-changed-fields' }, changedFields));
      }
      info.appendChild(el('p', { class: 'pending-queued-at' }, timeAgo(item.queued_at)));
      row.appendChild(info);
      list.appendChild(row);
    });
    changesSection.appendChild(list);

    // Push selected button
    const pushBtn = el('button', { class: 'btn-filled', type: 'button' },
      `Push ${queueCount} selected`);
    pushBtn.addEventListener('click', async () => {
      const selectedIds   = [...document.querySelectorAll('.pending-cb:checked')]
        .map(cb => parseInt(cb.dataset.queueId, 10));
      const selectedItems = queue.filter(q => selectedIds.includes(q.id));
      if (!selectedItems.length) { showToast('No items selected'); return; }

      pushBtn.disabled    = true;
      pushBtn.textContent = 'Pushing…';
      try {
        const { okIds, errors } = await sync.pushItems(API, selectedItems);
        if (okIds.length > 0) await db.clearQueueItems(okIds);
        showToast(errors.length
          ? `Pushed ${okIds.length} — ${errors.length} failed`
          : `Pushed ${okIds.length} edit${okIds.length !== 1 ? 's' : ''} to server`);
        await loadPendingIds();
        const grid = $('gallery-grid');
        if (grid) { grid.remove(); state.gallery.offset = 0; }
        setTimeout(() => renderSyncPage(container), 400);
      } catch {
        showToast('Push failed — check your server connection');
        updatePushBtn();
      }
    });
    changesSection.appendChild(pushBtn);
    page.appendChild(changesSection);

    function updatePushBtn() {
      const total   = document.querySelectorAll('.pending-cb').length;
      const checked = document.querySelectorAll('.pending-cb:checked').length;
      pushBtn.disabled    = checked === 0;
      pushBtn.textContent = checked > 0 ? `Push ${checked} selected` : 'Push selected';
      selectAllCb.indeterminate = checked > 0 && checked < total;
      selectAllCb.checked       = checked === total;
    }
  }

  // --- Progress area (hidden until full sync starts) ---
  const progressArea = el('div', { class: 'sync-progress-area hidden' });
  const bar  = el('div', { class: 'sync-progress-bar' });
  const fill = el('div', { class: 'sync-progress-fill' });
  bar.appendChild(fill);
  const progressText = el('p', { class: 'sync-progress-text' }, 'Preparing…');
  progressArea.append(bar, progressText);
  page.appendChild(progressArea);

  // --- Full sync button ---
  const syncBtn = el('button', { class: 'btn-filled sync-btn', type: 'button' },
    queueCount > 0 ? 'Full sync' : 'Sync now');
  syncBtn.addEventListener('click', async () => {
    syncBtn.disabled = true;
    progressArea.classList.remove('hidden');
    fill.style.width = '0%';

    try {
      const result = await sync.run(API, ({ phase, pct, done, total, count }) => {
        if (phase === 'push') {
          fill.style.width = '3%';
          progressText.textContent = `Pushing ${count} queued edit${count !== 1 ? 's' : ''}…`;
        } else if (phase === 'uploads') {
          fill.style.width = '5%';
          progressText.textContent = `Uploading photos: ${done} / ${total}`;
        } else if (phase === 'data') {
          fill.style.width = '8%';
          progressText.textContent = 'Downloading stamp data…';
        } else {
          fill.style.width = `${8 + Math.round(pct * 0.92)}%`;
          progressText.textContent = `Caching images: ${done} / ${total}`;
        }
      });
      fill.style.width = '100%';
      progressText.textContent = 'Done!';
      if (result.imagesSkipped) {
        notify('warn', `Synced ${result.stamps.toLocaleString()} stamps · images need HTTPS`);
      } else {
        showToast(`Synced ${result.stamps.toLocaleString()} stamps · ${result.images.toLocaleString()} images`);
        db.logEvent('info', `Synced ${result.stamps} stamps, ${result.images} images`).catch(() => {});
      }
      state.filters = null;
      await loadPendingIds();
      await refreshCaptureBadge();
      const grid = $('gallery-grid');
      if (grid) { grid.remove(); state.gallery.offset = 0; }
      setTimeout(() => renderSyncPage(container), 600);
    } catch (err) {
      notify('error', `Sync failed — ${err.message || 'check your server connection'}`, 6000);
      syncBtn.disabled = false;
      progressArea.classList.add('hidden');
    }
  });
  page.appendChild(syncBtn);

  page.appendChild(el('p', { class: 'sync-help-text' },
    'Join the laptop’s hotspot and tap Sync now to download your collection for offline use.'
  ));

  // --- Capture preferences ---
  const rapidRow = el('div', { class: 'edit-toggle-row', style: 'margin-top:20px' });
  const rapidCb  = el('input', { type: 'checkbox', class: 'edit-toggle', id: 'pref-rapid' });
  rapidCb.checked = rapidCapture;
  rapidCb.addEventListener('change', async () => {
    rapidCapture = rapidCb.checked;
    try { await db.setPref('rapid_capture', rapidCapture); } catch {}
  });
  rapidRow.append(
    rapidCb,
    el('label', { class: 'edit-toggle-label', for: 'pref-rapid' },
       'Rapid capture — reopen the camera after each photo'),
  );
  page.appendChild(rapidRow);

  // --- Recent activity ---
  let events = [];
  try { events = await db.getEvents(); } catch { /* store absent on an older DB */ }

  const activity = el('div', { class: 'activity-section' });
  const actHdr   = el('div', { class: 'activity-header' });
  actHdr.appendChild(el('h3', { class: 'pending-title' }, 'Recent activity'));
  if (events.length > 0) {
    const clearBtn = el('button', { class: 'btn-text', type: 'button' }, 'Clear');
    clearBtn.addEventListener('click', async () => {
      try { await db.clearEvents(); } catch {}
      renderSyncPage(container);
    });
    actHdr.appendChild(clearBtn);
  }
  activity.appendChild(actHdr);

  if (events.length === 0) {
    activity.appendChild(el('p', { class: 'sync-help-text' }, 'Nothing to report.'));
  } else {
    const list = el('div', { class: 'activity-list' });
    events.forEach(ev => {
      const row = el('div', { class: `activity-item activity-${ev.level}` });
      row.appendChild(el('span', { class: 'activity-msg' }, ev.message));
      row.appendChild(el('span', { class: 'activity-time' },
        new Date(ev.at).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' })));
      list.appendChild(row);
    });
    activity.appendChild(list);
  }
  page.appendChild(activity);

  container.innerHTML = '';
  container.appendChild(page);
}

// ============================================================
// Boot
// ============================================================
// Point the status-bar colour at the same token the top app bar uses, so the
// brand-hue knob in main.css reaches the phone's own chrome instead of needing
// a hex kept in sync by hand in index.html.
function syncThemeColor() {
  const bar = getComputedStyle(document.documentElement)
    .getPropertyValue('--c-top-bar-top').trim();
  const meta = document.querySelector('meta[name="theme-color"]');
  if (bar && meta) meta.setAttribute('content', bar);
}
syncThemeColor();

// The capture button lives in the bottom nav rather than inside a view, so it
// is wired once here instead of on every gallery render.
setupCapture();
router();
