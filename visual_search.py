"""
Local visual search — match a query image against images already in the
collection, entirely offline.

Runs on opencv and Pillow, both already required by the app. There is no model
file, no network call and no service dependency: everything here is arithmetic
over local pixels. That is a deliberate choice rather than an accident of
scope. Candidate pools are small (see below), so the large-N machinery that
would justify a learned embedding — a model download, a vector column, a
full-collection backfill — would buy nothing but a maintenance burden.

Two fingerprints per image, answering two different questions:

    perceptual hash (dHash, 64 bit)
        "Is this literally the same picture?" Cheap, order-independent, and
        robust to re-encoding and mild exposure shifts. Compared by Hamming
        distance; below VISUAL_DUP_THRESHOLD the images are near-duplicates.

    ORB keypoints + descriptors
        "Is this the same stamp?" Keypoint matching survives rotation, scale
        and cropping, and a RANSAC homography then throws out matches that are
        not geometrically consistent. This is instance matching, which is the
        actual question when identifying a stamp — as opposed to the semantic
        similarity ("also a flower") a CLIP-style embedding would give.

The expensive half is fingerprinting, so results are cached in a SQLite file
keyed by path and mtime (see config.VISUAL_CACHE_FILE). The cache is derived
data: deleting it costs one rebuild, never correctness.

Callers narrow the pool before matching — by country and face value, the same
way a Colnect search is entered. That matters more for accuracy than speed:
brute-force scoring over the whole collection would still be fast, but a
smaller pool removes the look-alikes that produce confident wrong answers.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass

import cv2
import numpy as np

import autocrop
from config import VISUAL_CACHE_FILE, VISUAL_DUP_THRESHOLD, VISUAL_NORM_SIZE
from logger import logger

# Keypoints retained per image. 500 is comfortably more than an engraved stamp
# yields at 512px, so this acts as a ceiling for pathological images rather
# than a target; raising it costs cache size without finding more real matches.
_ORB_FEATURES = 500

# Lowe's ratio test. A descriptor match counts only if the best candidate is
# clearly better than the runner-up, which is what suppresses the repeated
# patterns — perforations, guilloche shading — that stamps are full of.
_RATIO = 0.75

# Below this many ratio-test survivors there is nothing for RANSAC to fit: a
# homography needs 4 correspondences, and one fitted from barely more than that
# is noise dressed up as a match.
_MIN_MATCHES_FOR_RANSAC = 10

# Reprojection tolerance, in normalised pixels, for a match to count as an
# inlier of the fitted homography.
_RANSAC_REPROJ = 5.0

# Subject normalisation — see _subject_box.
#
# Resolution the subject search runs at. The box only needs to be right to a
# few pixels before it is mapped back up, and working small keeps this in the
# low milliseconds even on a 12 MP phone photo.
_SUBJECT_WORK = 256

# Detail-energy percentile that separates stamp from mat. The mat is the
# majority of a loosely framed photo, so the cut sits high.
_SUBJECT_PERCENTILE = 80

# Margin kept around the detected subject, as a fraction of its own size. A
# stamp's perforated edge carries real keypoints, so the box is not tightened
# onto the design alone.
_SUBJECT_PAD = 0.04

# Only trim when the subject is clearly smaller than the frame. Above this
# share the picture is already a tight crop, and trimming it further would
# move the two sides of a comparison apart rather than together — the failure
# this whole step exists to prevent.
_SUBJECT_MAX_AREA = 0.80

# Bumped whenever a change here would produce different fingerprints for the
# same file. Cached rows carrying another version are rebuilt rather than
# compared against fresh ones, which would silently score near zero.
_FINGERPRINT_VERSION = 2


@dataclass(frozen=True)
class Fingerprint:
    """The cached fingerprints for one image.

    `keypoints` and `descriptors` are parallel arrays — row i of one describes
    the same feature as row i of the other. Both are needed: descriptors say
    which features correspond, coordinates say whether that correspondence is
    geometrically possible.
    """
    path: str
    phash: int
    keypoints: np.ndarray | None    # (N, 2) float32 — x, y in normalised space
    descriptors: np.ndarray | None  # (N, 32) uint8

    @property
    def feature_count(self) -> int:
        return 0 if self.descriptors is None else int(self.descriptors.shape[0])


@dataclass(frozen=True)
class Match:
    """One scored candidate, as returned by search()."""
    path: str
    score: float          # 0..1, geometric-inlier ratio; higher is better
    inliers: int          # matches consistent with the fitted homography
    hamming: int          # perceptual-hash distance, 0..64
    is_duplicate: bool    # same picture, not merely the same stamp
    verified: bool        # score came from RANSAC, not the sparse fallback

    @property
    def confidence(self) -> str:
        """Coarse label for the UI.

        Thresholds are calibrated in tools/eval_visual_search.py against the
        real collection; treat them as tuned constants, not arbitrary ones.
        """
        if self.is_duplicate:
            return "duplicate"
        if not self.verified:
            return "weak"
        if self.inliers >= 25 and self.score >= 0.12:
            return "strong"
        if self.inliers >= 12:
            return "possible"
        return "weak"


# ----------------------------------------------------------------------
# Fingerprinting
# ----------------------------------------------------------------------

def _detail_box(gray: np.ndarray) -> tuple[int, int, int, int] | None:
    """Cheap subject box: the largest connected region of gradient energy.

    A stamp is detailed and a mat is flat. This is milliseconds and always
    answers, which is what makes it usable both as the gate in front of the
    expensive detector and as the fallback behind it.

    Returns None when the detail already fills the frame (a tight crop, so
    there is nothing to gain) or when there is no detail to find.
    """
    h, w = gray.shape[:2]
    scale = _SUBJECT_WORK / max(h, w)
    if scale < 1.0:
        small = cv2.resize(gray, (max(8, int(w * scale)), max(8, int(h * scale))),
                           interpolation=cv2.INTER_AREA)
    else:
        small, scale = gray, 1.0

    gx = cv2.Sobel(small, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(small, cv2.CV_32F, 0, 1, ksize=3)
    # Blurring the magnitude turns per-pixel texture into a region, so the
    # components below are areas of detail rather than strings of edge pixels.
    energy = cv2.GaussianBlur(cv2.magnitude(gx, gy), (0, 0), 3)
    threshold = max(float(np.percentile(energy, _SUBJECT_PERCENTILE)) * 0.5, 1.0)
    mask = (energy > threshold).astype(np.uint8)

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)

    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, 8)
    if count <= 1:
        return None                     # no detail anywhere; nothing to trim to

    largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    x, y, bw, bh, _area = stats[largest]

    inv = 1.0 / scale
    x1 = max(0, int((x - bw * _SUBJECT_PAD) * inv))
    y1 = max(0, int((y - bh * _SUBJECT_PAD) * inv))
    x2 = min(w, int((x + bw * (1 + _SUBJECT_PAD)) * inv))
    y2 = min(h, int((y + bh * (1 + _SUBJECT_PAD)) * inv))

    if x2 - x1 < 16 or y2 - y1 < 16:
        return None                     # a speck, not a stamp
    if (x2 - x1) * (y2 - y1) >= _SUBJECT_MAX_AREA * w * h:
        return None                     # already a tight crop; leave it alone
    return x1, y1, x2, y2


def _subject_box(colour: np.ndarray, gray: np.ndarray) -> tuple[int, int, int, int] | None:
    """Bounding box of the stamp within a frame, or None to use the whole frame.

    Normalising the *frame* to a fixed size is not enough, because it leaves
    the stamp itself at whatever scale the photographer framed it. A tight crop
    and a photo of the same stamp on a wide mat then produce descriptors at
    subject scales that differ several times over, and ORB's pyramid does not
    stretch that far. Measured over the collection, moving a stamp from 95% of
    the frame to 50% cut retrieval from 20/20 to 12/20 and the match score from
    0.47 to 0.017. That is the "Find Similar misses the obvious match" failure,
    because storage/images holds tight crops (autocrop declines on 53 of 60 of
    them precisely because they are already tight) while a capture it declined
    keeps its whole mat — median subject fill in storage/incoming is 47%.

    Two detectors, because neither alone is enough:

        _detail_box   milliseconds, always answers, approximate
        autocrop      ~390ms, materially more accurate (20/20 against 15/20 at
                      the harshest framing), but declines on roughly half of
                      real loose captures — exactly the frames needing it most

    So the cheap one gates and backstops the expensive one: it decides whether
    the frame is loose enough to be worth any further work, autocrop is asked
    for a better box when it is, and its answer is used when it declines.
    Collection images, which are overwhelmingly tight, pay only the cheap check.
    """
    rough = _detail_box(gray)
    if rough is None:
        return None                     # already tight, or no subject to find

    try:
        fine = autocrop.detect_in(colour)
    except Exception as e:
        logger.debug(f"visual_search: autocrop declined to run: {e}")
        fine = None
    if fine is None:
        return rough

    x1, y1, x2, y2 = fine
    if x2 - x1 < 16 or y2 - y1 < 16:
        return rough
    return fine


def _load_normalized(path: str) -> np.ndarray | None:
    """Read an image as greyscale, trimmed to the stamp and scaled so its
    longest edge is VISUAL_NORM_SIZE.

    Normalising size is what makes descriptors comparable at all: the
    collection holds everything from 222px crops to 4000px phone captures, and
    ORB keypoint scale is measured in pixels. Aspect ratio is preserved, so a
    stamp is never squashed into matching a differently-shaped one.

    The trim (see _subject_box) is what makes that normalisation mean the same
    thing for both sides of a comparison — without it the frame is normalised
    but the stamp inside it is not.
    """
    # cv2.imread cannot open a path containing non-ASCII characters on Windows.
    # Reading the bytes ourselves and decoding from memory sidesteps the
    # platform's locale handling entirely.
    try:
        data = np.fromfile(path, dtype=np.uint8)
    except OSError as e:
        logger.warning(f"visual_search: cannot read {path}: {e}")
        return None
    if data.size == 0:
        return None

    # Decoded in colour, then converted, because the subject detectors need
    # colour: autocrop segments the mat in Lab space. Matching itself only ever
    # sees the greyscale result.
    colour = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if colour is None:
        logger.warning(f"visual_search: cannot decode {path}")
        return None
    img = cv2.cvtColor(colour, cv2.COLOR_BGR2GRAY)

    box = _subject_box(colour, img)
    if box is not None:
        x1, y1, x2, y2 = box
        img = img[y1:y2, x1:x2]

    h, w = img.shape[:2]
    longest = max(h, w)
    if longest > VISUAL_NORM_SIZE:
        scale = VISUAL_NORM_SIZE / longest
        img = cv2.resize(
            img, (max(1, int(w * scale)), max(1, int(h * scale))),
            interpolation=cv2.INTER_AREA,
        )
    return img


def _compute_phash(img: np.ndarray) -> int:
    """64-bit difference hash: each bit is one horizontal gradient sign.

    dHash rather than a DCT-based pHash because it depends on local gradients
    instead of global frequency content, which makes it markedly less sensitive
    to the border and background variation between an in-hand photo and a scan
    of the same stamp.
    """
    small = cv2.resize(img, (9, 8), interpolation=cv2.INTER_AREA)
    diff = small[:, 1:] > small[:, :-1]
    return int.from_bytes(np.packbits(diff.flatten()).tobytes(), "big")


def _compute_orb(img: np.ndarray) -> tuple[np.ndarray | None, np.ndarray | None]:
    orb = cv2.ORB_create(nfeatures=_ORB_FEATURES)
    kps, descriptors = orb.detectAndCompute(img, None)
    if descriptors is None or len(kps) == 0:
        return None, None
    coords = np.array([kp.pt for kp in kps], dtype=np.float32)
    return coords, descriptors


def fingerprint(path: str) -> Fingerprint | None:
    """Compute both fingerprints for one image. Returns None if unreadable."""
    img = _load_normalized(path)
    if img is None:
        return None
    coords, descriptors = _compute_orb(img)
    return Fingerprint(path, _compute_phash(img), coords, descriptors)


# ----------------------------------------------------------------------
# Cache
# ----------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fingerprints (
    path       TEXT PRIMARY KEY,
    mtime      REAL    NOT NULL,
    phash      INTEGER NOT NULL,
    kp         BLOB,
    orb        BLOB,
    rows       INTEGER NOT NULL DEFAULT 0,
    version    INTEGER NOT NULL DEFAULT 0
);
"""

# SQLite connections cannot be shared across threads, and the UI may build the
# cache on a worker while the main thread queries it. One connection per thread
# keeps that safe without serialising every read behind a lock.
_local = threading.local()


def _connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        directory = os.path.dirname(VISUAL_CACHE_FILE)
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(VISUAL_CACHE_FILE)
        conn.execute(_SCHEMA)
        _migrate(conn)
        conn.commit()
        _local.conn = conn
    return conn


def _migrate(conn) -> None:
    """Bring an older cache file up to the current schema.

    The cache is derived data, so the cheap fix is always available: anything
    that cannot be carried forward is dropped and recomputed. A cache written
    before the version column existed holds fingerprints from before subject
    normalisation, and those are not comparable with current ones — a v1 row
    scored against a v2 query reads as "not the same stamp" rather than as an
    error, which is the worst way for this to fail.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(fingerprints)")}
    if "version" in columns:
        return
    conn.execute("DROP TABLE IF EXISTS fingerprints")
    conn.execute(_SCHEMA)
    logger.info(
        "visual_search: cache predates subject normalisation — cleared, it "
        "will rebuild as images are searched (or run tools.build_visual_cache)"
    )


def _key(path: str) -> str:
    """Cache key: absolute, normalised path.

    associate_image() moves files between storage/incoming and storage/images,
    so the same picture is seen under more than one relative path over its
    life. Normalising keeps one row per file rather than one per location it
    has occupied.
    """
    return os.path.normcase(os.path.abspath(path))


def _phash_to_db(value: int) -> int:
    """Map a 64-bit unsigned hash into SQLite's signed INTEGER range.

    dHash uses all 64 bits, but SQLite integers are signed, so any hash with
    the top bit set overflows on insert. Reinterpreting the same bits as
    two's-complement is lossless and keeps the column a plain INTEGER — the
    bit pattern, which is all Hamming distance cares about, is unchanged.
    """
    return value - (1 << 64) if value >= (1 << 63) else value


def _phash_from_db(value: int) -> int:
    return value + (1 << 64) if value < 0 else value


def _pack(fp: Fingerprint) -> tuple[bytes | None, bytes | None, int]:
    if fp.descriptors is None or fp.keypoints is None or len(fp.descriptors) == 0:
        return None, None, 0
    return (
        fp.keypoints.astype(np.float32).tobytes(),
        fp.descriptors.astype(np.uint8).tobytes(),
        int(fp.descriptors.shape[0]),
    )


def _unpack(kp_blob, orb_blob, rows):
    if not orb_blob or not kp_blob or rows <= 0:
        return None, None
    coords = np.frombuffer(kp_blob, dtype=np.float32).reshape(rows, 2)
    desc = np.frombuffer(orb_blob, dtype=np.uint8).reshape(rows, 32)
    return coords, desc


def get_fingerprint(path: str) -> Fingerprint | None:
    """Fingerprint for one image, from cache when it is still valid.

    A cache row is stale as soon as the file's mtime moves — which is exactly
    what happens when the crop or rotate buttons rewrite an image in place, so
    an edited stamp is re-fingerprinted rather than matched on its old pixels.
    """
    key = _key(path)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None

    conn = _connect()
    row = conn.execute(
        "SELECT mtime, phash, kp, orb, rows FROM fingerprints "
        "WHERE path = ? AND version = ?", (key, _FINGERPRINT_VERSION)
    ).fetchone()
    if row is not None and abs(row[0] - mtime) < 1e-6:
        coords, desc = _unpack(row[2], row[3], row[4])
        return Fingerprint(path, _phash_from_db(row[1]), coords, desc)

    fp = fingerprint(path)
    if fp is None:
        return None
    _store(conn, key, mtime, fp)
    conn.commit()
    return fp


def _store(conn, key: str, mtime: float, fp: Fingerprint) -> None:
    kp_blob, orb_blob, rows = _pack(fp)
    conn.execute(
        "INSERT OR REPLACE INTO fingerprints "
        "(path, mtime, phash, kp, orb, rows, version) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (key, mtime, _phash_to_db(fp.phash), kp_blob, orb_blob, rows,
         _FINGERPRINT_VERSION),
    )


def build_cache(paths, progress=None) -> tuple[int, int]:
    """Fingerprint every path not already cached.

    Returns (processed, failed). `progress` is called as (done, total) so a
    caller can drive a progress bar; it runs on the calling thread.

    Commits in batches rather than per row — each commit is an fsync, and over
    a few thousand images that difference is minutes.
    """
    paths = list(paths)
    total = len(paths)
    conn = _connect()
    processed = failed = 0

    for i, path in enumerate(paths, 1):
        key = _key(path)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            failed += 1
            if progress:
                progress(i, total)
            continue

        row = conn.execute(
            "SELECT mtime FROM fingerprints WHERE path = ? AND version = ?",
            (key, _FINGERPRINT_VERSION),
        ).fetchone()
        if row is not None and abs(row[0] - mtime) < 1e-6:
            if progress:
                progress(i, total)
            continue

        fp = fingerprint(path)
        if fp is None:
            failed += 1
        else:
            _store(conn, key, mtime, fp)
            processed += 1
            if processed % 200 == 0:
                conn.commit()
        if progress:
            progress(i, total)

    conn.commit()
    logger.info(f"visual_search: cache build — {processed} added, {failed} failed")
    return processed, failed


def load_fingerprints(paths) -> dict[str, Fingerprint]:
    """Bulk-load cached fingerprints, keyed by the caller's original path.

    Reads the whole candidate pool in one query instead of one per image. Paths
    missing from the cache are fingerprinted on demand and stored, so a search
    still works on a cold or partially built cache — just more slowly.
    """
    paths = list(paths)
    if not paths:
        return {}

    conn = _connect()
    by_key = {_key(p): p for p in paths}
    out: dict[str, Fingerprint] = {}

    # Chunked to stay under SQLite's variable limit (999 by default).
    keys = list(by_key)
    for start in range(0, len(keys), 500):
        chunk = keys[start:start + 500]
        placeholders = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT path, mtime, phash, kp, orb, rows FROM fingerprints "
            f"WHERE version = ? AND path IN ({placeholders})",
            [_FINGERPRINT_VERSION, *chunk],
        ):
            original = by_key[row[0]]
            try:
                if abs(row[1] - os.path.getmtime(original)) > 1e-6:
                    continue  # stale; falls through to the rebuild below
            except OSError:
                continue
            coords, desc = _unpack(row[3], row[4], row[5])
            out[original] = Fingerprint(original, _phash_from_db(row[2]), coords, desc)

    missing = [p for p in paths if p not in out]
    if missing:
        logger.info(f"visual_search: fingerprinting {len(missing)} uncached image(s)")
        for path in missing:
            fp = get_fingerprint(path)
            if fp is not None:
                out[path] = fp
    return out


def prune_cache(valid_paths) -> int:
    """Drop cache rows whose image is no longer part of the collection."""
    valid = {_key(p) for p in valid_paths}
    conn = _connect()
    stale = [r[0] for r in conn.execute("SELECT path FROM fingerprints") if r[0] not in valid]
    conn.executemany("DELETE FROM fingerprints WHERE path = ?", [(p,) for p in stale])
    conn.commit()
    return len(stale)


def cache_size() -> int:
    return _connect().execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]


# ----------------------------------------------------------------------
# Matching
# ----------------------------------------------------------------------

def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _score_pair(query: Fingerprint, cand: Fingerprint) -> tuple[float, int, bool]:
    """Score a candidate against the query.

    Returns (ratio, inliers, verified). The ratio is inliers over the smaller
    descriptor set, so a sparse query cannot be penalised for being matched
    against a densely featured candidate.

    The RANSAC step is what separates this from a bag-of-features count. Stamps
    of the same era share border patterns, lettering and shading, and those
    produce plenty of honest descriptor matches that no single homography can
    explain. Requiring one transform to account for the matches is what makes
    "same design" distinguishable from "same style".
    """
    qd, cd = query.descriptors, cand.descriptors
    if qd is None or cd is None or len(qd) < 2 or len(cd) < 2:
        return 0.0, 0, False

    try:
        knn = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(qd, cd, k=2)
    except cv2.error as e:
        logger.warning(f"visual_search: match failed for {cand.path}: {e}")
        return 0.0, 0, False

    good = [pair[0] for pair in knn
            if len(pair) == 2 and pair[0].distance < _RATIO * pair[1].distance]

    denom = min(len(qd), len(cd)) or 1
    if len(good) < _MIN_MATCHES_FOR_RANSAC:
        # Too few correspondences to verify geometrically. Report the raw rate
        # as a weak signal rather than zero, so a genuine match on a sparse
        # image is not silently dropped — flagged unverified so the UI can say
        # so, and so confidence never rises above "weak".
        return len(good) / denom, len(good), False

    src = np.float32([query.keypoints[m.queryIdx] for m in good]).reshape(-1, 1, 2)
    dst = np.float32([cand.keypoints[m.trainIdx] for m in good]).reshape(-1, 1, 2)
    _, mask = cv2.findHomography(src, dst, cv2.RANSAC, _RANSAC_REPROJ)
    if mask is None:
        return 0.0, 0, True

    inliers = int(mask.sum())
    return inliers / denom, inliers, True


def search(query_path: str, candidate_paths, limit: int = 25) -> list[Match]:
    """Rank `candidate_paths` by visual similarity to `query_path`.

    Candidates are expected to be pre-filtered by the caller (country and face
    value), which is where the accuracy comes from — see the module docstring.
    Passing the whole collection works and stays responsive, but look-alikes
    from unrelated countries will compete with the real answer.

    Near-duplicates are surfaced by perceptual hash and sorted to the top
    regardless of their keypoint score: "you already have this picture" is a
    different and more urgent answer than "this resembles that stamp".
    """
    query = get_fingerprint(query_path)
    if query is None:
        logger.warning(f"visual_search: unreadable query image {query_path}")
        return []

    # Never rank the query against itself — it is in the pool whenever the
    # search is run from an image already attached to a stamp.
    qkey = _key(query_path)
    candidates = [p for p in candidate_paths if _key(p) != qkey]

    prints = load_fingerprints(candidates)
    matches: list[Match] = []
    for path, fp in prints.items():
        dist = hamming(query.phash, fp.phash)
        is_dup = dist <= VISUAL_DUP_THRESHOLD
        ratio, inliers, verified = _score_pair(query, fp)
        matches.append(Match(path, ratio, inliers, dist, is_dup, verified))

    matches.sort(key=_sort_key)
    return matches[:limit]


def _sort_key(m: Match) -> tuple[int, float, float]:
    """Duplicates first, ordered by how identical they are; then everything
    else by geometric score, with hash distance breaking ties.

    Both branches return the same tuple shape so the comparison stays
    well-defined across the two groups.
    """
    if m.is_duplicate:
        return (0, float(m.hamming), 0.0)
    return (1, -m.score, float(m.hamming))
