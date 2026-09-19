// db.js — IndexedDB persistence layer

const DB_NAME    = 'stamp-collection';
// v2 adds the 'uploads' store — photos captured while the laptop is unreachable.
// v3 adds 'event_log' — warnings and errors, reviewable on the Sync page after
// the toast that announced them has gone.
const DB_VERSION = 3;

// Oldest entries are dropped past this; the log is a recent-history view, not
// an audit trail.
const EVENT_LOG_MAX = 60;

let _db = null;

function openDB() {
  if (_db) return Promise.resolve(_db);
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);

    req.onupgradeneeded = e => {
      const db = e.target.result;
      if (!db.objectStoreNames.contains('stamps')) {
        db.createObjectStore('stamps', { keyPath: 'id' });
      }
      if (!db.objectStoreNames.contains('sync_meta')) {
        db.createObjectStore('sync_meta', { keyPath: 'key' });
      }
      if (!db.objectStoreNames.contains('edit_queue')) {
        const eq = db.createObjectStore('edit_queue', { keyPath: 'id', autoIncrement: true });
        eq.createIndex('stamp_id', 'stamp_id', { unique: false });
      }
      if (!db.objectStoreNames.contains('uploads')) {
        db.createObjectStore('uploads', { keyPath: 'id', autoIncrement: true });
      }
      if (!db.objectStoreNames.contains('event_log')) {
        db.createObjectStore('event_log', { keyPath: 'id', autoIncrement: true });
      }
    };

    req.onsuccess = e => { _db = e.target.result; resolve(_db); };
    req.onerror   = ()  => reject(req.error);
  });
}

function idbReq(r) {
  return new Promise((resolve, reject) => {
    r.onsuccess = () => resolve(r.result);
    r.onerror   = () => reject(r.error);
  });
}

async function store(name, mode = 'readonly') {
  const db = await openDB();
  return db.transaction(name, mode).objectStore(name);
}

export const db = {

  // --- Stamp data ---

  async saveAllStamps(stamps) {
    const database = await openDB();
    const txn = database.transaction('stamps', 'readwrite');
    const st  = txn.objectStore('stamps');
    stamps.forEach(s => st.put(s));
    return new Promise((res, rej) => {
      txn.oncomplete = res;
      txn.onerror    = () => rej(txn.error);
    });
  },

  async getStamp(id) {
    return idbReq((await store('stamps')).get(id));
  },

  async getStamps({ search = '', country = '', theme = '', sort = 'date', limit = 50, offset = 0 } = {}) {
    const all = await idbReq((await store('stamps')).getAll());

    let list = all;
    if (search) {
      const q = search.toLowerCase();
      list = list.filter(s =>
        (s.title        || '').toLowerCase().includes(q) ||
        (s.scott_number || '').toLowerCase().includes(q) ||
        (s.country      || '').toLowerCase().includes(q) ||
        (s.series       || '').toLowerCase().includes(q)
      );
    }
    if (country) list = list.filter(s => s.country === country);
    if (theme)   list = list.filter(s => (s.themes || []).includes(theme));

    const sorts = {
      date:    (a, b) => (b.added_to_db || '').localeCompare(a.added_to_db || ''),
      oldest:  (a, b) => (a.added_to_db || '').localeCompare(b.added_to_db || ''),
      title:   (a, b) => (a.title        || '').localeCompare(b.title        || ''),
      country: (a, b) => (a.country      || '').localeCompare(b.country      || ''),
      scott:   (a, b) => (a.scott_number || '').localeCompare(b.scott_number || '', undefined, { numeric: true }),
    };
    list.sort(sorts[sort] ?? sorts.date);

    return {
      stamps: list.slice(offset, offset + limit).map(s => ({
        id:             s.id,
        title:          s.title,
        scott_number:   s.scott_number,
        country:        s.country,
        image_filename: (s.images || [])[0] ?? null,
        conditions:     (s.copies || []).map(c => c.condition),
      })),
      total:  list.length,
      offset,
      limit,
    };
  },

  async countStamps() {
    return idbReq((await store('stamps')).count());
  },

  async getCountries() {
    const all = await idbReq((await store('stamps')).getAll());
    return [...new Set(all.map(s => s.country).filter(Boolean))].sort();
  },

  async getThemes() {
    const all = await idbReq((await store('stamps')).getAll());
    const set = new Set();
    all.forEach(s => (s.themes || []).forEach(t => set.add(t)));
    return [...set].sort();
  },

  // --- Sync metadata ---

  async getLastSync() {
    const meta = await idbReq((await store('sync_meta')).get('last_sync'));
    return meta?.value ?? null;
  },

  async setLastSync(isoString) {
    return idbReq((await store('sync_meta', 'readwrite')).put({ key: 'last_sync', value: isoString }));
  },

  // --- Edit queue (wired up in the edit task) ---

  async queueEdit(stampId, changes) {
    return idbReq((await store('edit_queue', 'readwrite')).add({
      stamp_id:  stampId,
      changes,
      queued_at: new Date().toISOString(),
    }));
  },

  async getEditQueue() {
    return idbReq((await store('edit_queue')).getAll());
  },

  async clearQueueItems(ids) {
    const database = await openDB();
    const st = database.transaction('edit_queue', 'readwrite').objectStore('edit_queue');
    ids.forEach(id => st.delete(id));
  },

  async applyEditLocally(stampId, changes) {
    const database = await openDB();
    const st    = database.transaction('stamps', 'readwrite').objectStore('stamps');
    const stamp = await idbReq(st.get(stampId));
    if (!stamp) return;
    return idbReq(st.put({ ...stamp, ...changes }));
  },

  // --- Capture upload queue ---
  // Photos taken while the laptop is unreachable. The Blob is stored as-is, so
  // the bytes that eventually reach the server are the camera's originals.

  async queueUpload(blob) {
    return idbReq((await store('uploads', 'readwrite')).add({
      blob,
      queued_at: new Date().toISOString(),
    }));
  },

  async getUploads() {
    return idbReq((await store('uploads')).getAll());
  },

  async countUploads() {
    return idbReq((await store('uploads')).count());
  },

  async deleteUploads(ids) {
    const database = await openDB();
    const st = database.transaction('uploads', 'readwrite').objectStore('uploads');
    ids.forEach(id => st.delete(id));
  },

  // --- Preferences ---
  // Kept in sync_meta under a pref_ prefix so a new setting needs no schema bump.

  async setPref(key, value) {
    return idbReq((await store('sync_meta', 'readwrite')).put({ key: `pref_${key}`, value }));
  },

  async getPref(key, fallback = null) {
    const meta = await idbReq((await store('sync_meta')).get(`pref_${key}`));
    return meta?.value ?? fallback;
  },

  // --- Event log ---
  // Toasts vanish after a few seconds; these persist so the Sync page can show
  // what went wrong with an upload or a sync after the fact.
  //
  // Each step below runs its own transaction on purpose: IndexedDB commits a
  // transaction as soon as it goes idle, and an await between two requests on
  // the same one is enough to lose it.

  async logEvent(level, message) {
    await idbReq((await store('event_log', 'readwrite')).add({
      level, message, at: new Date().toISOString(),
    }));

    const keys = await idbReq((await store('event_log')).getAllKeys());
    if (keys.length > EVENT_LOG_MAX) {
      // Keys autoIncrement, so the lowest are the oldest.
      const excess = keys.slice(0, keys.length - EVENT_LOG_MAX);
      const st = await store('event_log', 'readwrite');
      excess.forEach(k => st.delete(k));
    }
  },

  async getEvents() {
    const all = await idbReq((await store('event_log')).getAll());
    return all.reverse();   // newest first
  },

  async clearEvents() {
    return idbReq((await store('event_log', 'readwrite')).clear());
  },

  // --- Reference data (themes list, locations, variant sets) for offline edit form ---

  async setRefData(data) {
    return idbReq((await store('sync_meta', 'readwrite')).put({ key: 'ref_data', value: data }));
  },

  async getRefData() {
    const meta = await idbReq((await store('sync_meta')).get('ref_data'));
    return meta?.value ?? null;
  },
};
