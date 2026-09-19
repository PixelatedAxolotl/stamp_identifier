// sync.js — Downloads all stamp data and images from the server into local storage

import { db } from './db.js';

const IMAGE_CACHE = 'stamp-images-v1';

export const sync = {
  // Upload one captured photo to the laptop's incoming folder.
  // Throws on a network failure (caller should queue it); returns the server's
  // reply for an HTTP error so the caller can tell "unreachable" from "rejected".
  async uploadCapture(API, blob) {
    const form = new FormData();
    // The server ignores this name and generates its own — it is here only
    // because multipart requires a filename field.
    form.append('file', blob, 'capture.jpg');
    const res = await fetch(`${API}/api/upload`, { method: 'POST', body: form });
    if (!res.ok) {
      let detail = `Server error ${res.status}`;
      try { detail = (await res.json()).detail || detail; } catch { /* keep default */ }
      return { ok: false, detail };
    }
    return { ok: true, ...(await res.json()) };
  },

  // Push any photos captured while the laptop was unreachable.
  // Returns { sent, failed }. A rejected photo (bad format) is dropped rather
  // than retried forever; only network failures leave items in the queue.
  async pushUploads(API, onProgress) {
    const pending = await db.getUploads();
    if (!pending.length) return { sent: 0, failed: 0 };

    const done = [];
    let sent = 0, failed = 0;
    for (const item of pending) {
      try {
        const r = await this.uploadCapture(API, item.blob);
        if (r.ok) { sent++; done.push(item.id); }
        else      { failed++; done.push(item.id); }
      } catch {
        break;   // still unreachable — leave this and the rest queued
      }
      onProgress?.({ phase: 'uploads', done: sent + failed, total: pending.length });
    }
    if (done.length) await db.deleteUploads(done);
    return { sent, failed };
  },

  // Push a specific list of queue items to the server.
  // Returns { okIds: number[], errors: object[] }
  async pushItems(API, items) {
    if (!items.length) return { okIds: [], errors: [] };
    const res = await fetch(`${API}/api/sync/push`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(items.map(q => ({
        stamp_id:  q.stamp_id,
        changes:   q.changes,
        queued_at: q.queued_at,
      }))),
    });
    if (!res.ok) throw new Error(`Server error ${res.status}`);
    const result = await res.json();
    const okIds  = items.filter((_, i) => result.results[i]?.status === 'ok').map(q => q.id);
    const errors = result.results.filter(r => r.status !== 'ok');
    return { okIds, errors };
  },

  // Full sync: push all pending edits, then pull all stamp data and images.
  async run(API, onProgress) {
    // 0. Push all queued edits first
    const queue = await db.getEditQueue();
    if (queue.length > 0) {
      onProgress?.({ phase: 'push', count: queue.length });
      try {
        const { okIds } = await this.pushItems(API, queue);
        if (okIds.length > 0) await db.clearQueueItems(okIds);
      } catch { /* network issue — continue with data pull */ }
    }

    // 0b. Push any photos captured while the laptop was unreachable
    try {
      await this.pushUploads(API, onProgress);
    } catch { /* network issue — they stay queued for next time */ }

    // 1. Download all stamp data
    onProgress?.({ phase: 'data', pct: 0 });
    const dataRes = await fetch(`${API}/api/sync/data`);
    if (!dataRes.ok) throw new Error(`Server error ${dataRes.status}`);
    const data = await dataRes.json();

    await db.saveAllStamps(data.stamps);
    await db.setLastSync(data.exported_at);
    await db.setRefData({
      themes:             (data.themes || []).map(t => t.name ?? t),
      physical_locations: data.physical_locations || [],
      variant_sets:       data.variant_sets       || [],
    });
    onProgress?.({ phase: 'data', pct: 100 });

    // 2. Download images into Cache API.
    // The Cache API is secure-context only, so `caches` is undefined when the app is
    // served over plain HTTP (e.g. a bare LAN IP with no TLS). Skip the image phase
    // rather than throwing — the stamp data above is already saved and usable.
    if (typeof caches === 'undefined') {
      return { stamps: data.stamps.length, images: 0, imagesSkipped: true };
    }

    const imgListRes = await fetch(`${API}/api/sync/images`);
    const imgList    = imgListRes.ok ? await imgListRes.json() : [];

    if (imgList.length > 0) {
      const cache = await caches.open(IMAGE_CACHE);
      let   done  = 0;

      for (const img of imgList) {
        const url      = `${API}/api/images/${encodeURIComponent(img.filename)}`;
        const existing = await cache.match(url);
        if (!existing) {
          try {
            const r = await fetch(url);
            if (r.ok) await cache.put(url, r);
          } catch { /* skip — continue with remaining images */ }
        }
        done++;
        onProgress?.({ phase: 'images', pct: Math.round((done / imgList.length) * 100), done, total: imgList.length });
      }
    }

    return { stamps: data.stamps.length, images: imgList.length, imagesSkipped: false };
  },
};
