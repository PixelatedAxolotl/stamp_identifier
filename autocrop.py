"""
Propose a crop rectangle around the stamp in a captured photo.

Runs on opencv and Pillow, both already required. Like visual_search, there is
no model file and no network call — the geometry is recoverable with plain
arithmetic once one assumption holds:

    the stamp never reaches the edge of the frame; a margin of backing mat is
    always visible all the way around it.

That premise is what makes this tractable, and it earns its keep twice. It means
the mat is a single connected region touching every frame edge, so it can be
flood-filled inward from the border and whatever the fill fails to reach is
foreground. And it means any blob that *does* touch the frame edge is, by
construction, not the stamp — so a neighbouring stamp half in shot, a finger, or
a pencil lying across the corner is rejected outright rather than merely
out-scored.

Flood-filling rather than thresholding is the other load-bearing choice. Two
threshold-based approaches were measured against storage/incoming and both
failed on the same class of image:

    Otsu over brightness      splits a stamp whose own ink is darker than the
                              split point, cropping to an interior panel
    Otsu / fixed multiple     same failure on pale stamps; tightening the
    over distance-from-mat    threshold to fix it collapsed the detection rate
                              from 118/150 to between 9 and 60

The fill follows the mat's own local gradient instead of any global cutoff, so a
pale stamp and a dark stamp are found by the same rule. On a 150-image sample it
accepted 106 and declined 44; of 24 accepted crops inspected by eye, 21 were
tight and correct.

detect() returns None whenever it cannot find a confident answer — most often
because the photo violates the premise and the stamp runs off an edge. Callers
are expected to fall back to a manual crop, which is why declining is the right
move: a wrong-but-confident box silently clips a stamp, and the file is
rewritten in place.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace

import cv2
import numpy as np
from PIL import Image, ImageOps

from config import (
    AUTOCROP_FLATTEN, AUTOCROP_FLATTEN_BORDER, AUTOCROP_MIN_AREA, AUTOCROP_PAD,
    AUTOCROP_REJECT_DOMINANCE, AUTOCROP_TOL,
)
from image_storage import snapshot_original
from logger import logger


@dataclass(frozen=True)
class Tuning:
    """Every knob the detector has, in one place.

    Defaults come from config, so the app and `python -m tools.autocrop_debug`
    agree unless the tool is told otherwise. Passing an instance to detect() is
    how you try a setting without editing config or exporting an env var:

        autocrop.detect(path, Tuning(tol=18, flatten=False))
        autocrop.detect(path, autocrop.DEFAULTS.but(pad=0.0))
    """

    # Per-channel Lab tolerance for the flood. Fixed-range, so this is the total
    # spread the mat is allowed rather than a per-step delta.
    tol: int = AUTOCROP_TOL

    # Margin kept around the detected stamp, as a fraction of the box's own size.
    pad: float = AUTOCROP_PAD

    # Blobs below this share of the frame are dust, perforation chips and JPEG
    # noise, never a stamp.
    min_area: float = AUTOCROP_MIN_AREA

    # How much larger an edge-touching blob may be than the chosen one before
    # the detector decides the real subject ran off frame and declines.
    reject_dominance: float = AUTOCROP_REJECT_DOMINANCE

    # Subtract a border-fitted illumination surface before segmenting. Turning
    # this off is the quickest way to tell a lighting problem from a framing one.
    flatten: bool = AUTOCROP_FLATTEN

    # Width of the border ring that surface is fitted to, per side.
    flatten_border: float = AUTOCROP_FLATTEN_BORDER

    # Longest edge the detection runs at. The mask only needs to be accurate to
    # a pixel or two here — the box is mapped back up afterwards — and working
    # small keeps a 12 MP phone photo under half a second.
    work: int = 900

    # How close to the frame edge a blob may come before it counts as touching
    # it. A couple of pixels absorbs the morphology's own bleed.
    edge_slack: int = 2

    # Spacing of the flood seeds along each edge, as a fraction of the shorter
    # side. Seeding only the four corners leaves a mat with a lighting gradient
    # partly unfilled, because the far side is out of tolerance from any corner.
    seed_step: float = 1 / 12

    # Sigma-clipping passes when fitting the illumination surface. Two refits
    # shed a stamp overhanging the ring; more just chases noise.
    flatten_refits: int = 3

    # Stop clipping if the surviving ring falls below this many pixels — a fit
    # from fewer is worse than the one already in hand.
    flatten_min_samples: int = 64

    # Share of the ring that must survive the pre-fit outlier clip for the
    # surface to be trusted. Below this the border is not mostly mat, so the
    # correction is skipped rather than fitted to a stamp.
    flatten_min_ring_fraction: float = 0.7

    def but(self, **changes) -> "Tuning":
        """A copy with some fields changed."""
        return replace(self, **changes)


DEFAULTS = Tuning()


def load_upright(path: str) -> np.ndarray | None:
    """Read an image as BGR with its EXIF orientation baked in.

    Every coordinate this module returns is in that upright space, which is the
    same space CropDialog displays and crops in. Phone photos are stored in the
    camera's native landscape buffer with an Orientation tag, so skipping this
    would hand back a box whose axes are swapped against the picture the user
    is looking at.
    """
    try:
        with Image.open(path) as im:
            upright = ImageOps.exif_transpose(im).convert("RGB")
            return cv2.cvtColor(np.array(upright), cv2.COLOR_RGB2BGR)
    except Exception as e:
        logger.warning(f"autocrop: cannot read {path}: {e}")
        return None


def _flatten_illumination(lab: np.ndarray, tuning: Tuning) -> np.ndarray:
    """Remove the mat's lighting gradient, so one tolerance fits the whole frame.

    A fixed-range flood asks "is this pixel within tol of its seed", which
    quietly assumes the mat is one colour. Under a lamp it is not: brightness
    ramps across the frame, bands between seeds fall outside tolerance, and what
    is left unfilled becomes foreground touching the frame edge — which is then
    rejected, and takes the real stamp's detection down with it. Measured by
    adding a synthetic ramp to frames that otherwise detect cleanly, a 120-level
    gradient cost 13 of 40 correct boxes and a uniform 120-level brightening cost
    19; with this step both hold at roughly 32 of 40.

    The premise that the border is mat is what makes the correction possible: fit
    a quadratic surface to the border ring, extrapolate it across the frame, and
    subtract. Sampling the corners alone would catch a simple diagonal ramp but
    not the off-centre falloff an angled lamp actually produces, which is why
    this fits a surface to the whole ring instead of a handful of points.

    The fit is sigma-clipped and refitted, because the ring is not always purely
    mat — a stamp overhanging it would otherwise drag the surface toward itself
    and leave a dent where the correction undershoots.
    """
    height, width = lab.shape[:2]
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    # Normalised and centred, so the design matrix stays well conditioned
    # regardless of frame size.
    xn, yn = xx / width - 0.5, yy / height - 0.5
    design = np.stack(
        [t.ravel() for t in (np.ones_like(xn), xn, yn, xn * yn, xn ** 2, yn ** 2)],
        axis=1,
    )

    ring = np.zeros((height, width), bool)
    bh = max(1, int(height * tuning.flatten_border))
    bw = max(1, int(width * tuning.flatten_border))
    ring[:bh] = ring[-bh:] = True
    ring[:, :bw] = ring[:, -bw:] = True
    in_ring = ring.ravel()

    out = np.empty(lab.shape, np.float32)
    for channel in range(3):
        values = lab[:, :, channel].ravel().astype(np.float32)

        # Reject gross outliers against the ring's own median BEFORE fitting.
        # Least squares has no resistance at all: a stamp overhanging the border
        # tilts the very first surface so far that every residual is large, the
        # spread below is inflated to match, and the sigma-clipping that is
        # supposed to remove the stamp then removes nothing. Clipping against
        # the median first is what keeps that from happening.
        ring_values = values[in_ring]
        ring_median = np.median(ring_values)
        ring_spread = np.median(np.abs(ring_values - ring_median)) + 1e-3
        selected = in_ring & (np.abs(values - ring_median) < 4 * 1.4826 * ring_spread)
        if selected.sum() < in_ring.sum() * tuning.flatten_min_ring_fraction:
            # Too little of the border looks like one surface, so it is not
            # mostly mat and there is nothing trustworthy to fit. Leave the
            # frame alone: the un-flattened path handles this by declining,
            # which is the right answer when the premise is broken.
            return np.clip(lab, 0, 255).astype(np.uint8)

        predicted = None
        for _ in range(tuning.flatten_refits):
            coefficients, *_ = np.linalg.lstsq(design[selected], values[selected], rcond=None)
            predicted = design @ coefficients
            residual = values - predicted
            # 1.4826 * MAD is the robust stand-in for a standard deviation.
            spread = np.median(np.abs(residual[selected])) + 1e-3
            keep = selected & (np.abs(residual) < 3 * 1.4826 * spread)
            if keep.sum() < tuning.flatten_min_samples:
                break       # too little ring survived to refit on; keep this fit
            selected = keep
        out[:, :, channel] = (values - predicted).reshape(height, width)

    # Residuals are signed and centred on zero; re-bias to the middle of the
    # byte range so the mat lands near 128 and floodFill can work in uint8.
    return np.clip(out + 128.0, 0, 255).astype(np.uint8)


def _foreground_mask(small: np.ndarray, tuning: Tuning) -> np.ndarray:
    """Flood the mat inward from the frame border; return what it did not reach."""
    lab = cv2.cvtColor(cv2.GaussianBlur(small, (5, 5), 0), cv2.COLOR_BGR2LAB)
    if tuning.flatten:
        lab = _flatten_illumination(lab, tuning)
    h, w = lab.shape[:2]

    # floodFill wants a mask two pixels larger than the image in each axis.
    filled = np.zeros((h + 2, w + 2), np.uint8)
    #
    # FIXED_RANGE compares every candidate pixel against the seed's colour. The
    # default compares it against the neighbour it is spreading from, which lets
    # the fill walk up a gradient in arbitrarily small steps — and the blur above
    # puts exactly such a ramp at every stamp edge, so a low-contrast stamp gets
    # flooded straight through and cropped to whatever bright panel sits inside
    # it. Measured over 150 frames, fixing the range lifts the 10th-percentile
    # kept area from 0.09 to 0.22 (far fewer clipped crops) at the cost of ~12
    # more declines, which fall back to a manual crop. A decline is cheap; a
    # confident bad crop rewrites the file.
    flags = 4 | cv2.FLOODFILL_MASK_ONLY | cv2.FLOODFILL_FIXED_RANGE | (255 << 8)
    step = max(1, int(min(h, w) * tuning.seed_step))
    seeds = (
        [(x, 0) for x in range(0, w, step)] + [(x, h - 1) for x in range(0, w, step)] +
        [(0, y) for y in range(0, h, step)] + [(w - 1, y) for y in range(0, h, step)]
    )
    for sx, sy in seeds:
        if filled[sy + 1, sx + 1]:
            continue    # already reached by an earlier seed
        # floodFill mutates its image argument even in MASK_ONLY mode.
        cv2.floodFill(lab.copy(), filled, (sx, sy), 0,
                      (tuning.tol,) * 3, (tuning.tol,) * 3, flags)

    fg = np.where(filled[1:-1, 1:-1] > 0, 0, 255).astype(np.uint8)

    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, k, iterations=1)
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, k, iterations=2)

    # Solidify each blob. A stamp whose centre matches the mat closely enough to
    # be flooded through a gap in its border would otherwise survive as a ring,
    # and a ring's bounding box is right only by accident.
    contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    solid = np.zeros_like(fg)
    cv2.drawContours(solid, contours, -1, 255, -1)
    return solid


def detect(path: str, tuning: Tuning | None = None,
           report: dict | None = None) -> tuple[int, int, int, int] | None:
    """Propose (x1, y1, x2, y2) around the stamp, or None to decline.

    Coordinates are pixels in the EXIF-upright image. None means no confident
    answer — the caller should fall back to a manual crop rather than treat it
    as an error. `tuning` defaults to the config-derived DEFAULTS.

    Pass a dict as `report` to have it filled in with why the answer came out
    the way it did; see detect_in. Only tools.autocrop_debug uses it.
    """
    image = load_upright(path)
    if image is None:
        if report is not None:
            report["outcome"] = "unreadable"
        return None
    return detect_in(image, tuning, report)


def detect_in(image: np.ndarray, tuning: Tuning | None = None,
              report: dict | None = None) -> tuple[int, int, int, int] | None:
    """detect() against an already-loaded upright BGR frame.

    `report`, when given, is populated with the blob counts and the reason for
    the outcome. It is an out-parameter rather than a return value so that the
    troubleshooting tool reads the same code path the app runs, instead of a
    reimplementation that could drift from it.
    """
    tuning = DEFAULTS if tuning is None else tuning
    height, width = image.shape[:2]

    scale = tuning.work / max(height, width) if max(height, width) > tuning.work else 1.0
    small = (cv2.resize(image, (int(width * scale), int(height * scale)),
                        interpolation=cv2.INTER_AREA)
             if scale != 1.0 else image)

    mask = _foreground_mask(small, tuning)
    mh, mw = mask.shape[:2]
    frame_area = mh * mw

    count, _, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    best, best_score, best_area = None, -1.0, 0
    largest_rejected = 0
    considered = touching = 0
    for i in range(1, count):
        x, y, w, h, area = stats[i]
        if area < frame_area * tuning.min_area:
            continue
        considered += 1
        slack = tuning.edge_slack
        if (x <= slack or y <= slack
                or x + w >= mw - 1 - slack or y + h >= mh - 1 - slack):
            # Touches the frame edge, so not the stamp (see module docstring).
            largest_rejected = max(largest_rejected, area)
            touching += 1
            continue
        cx, cy = centroids[i]
        dx, dy = abs(cx - mw / 2) / (mw / 2), abs(cy - mh / 2) / (mh / 2)
        centrality = 1.0 - min(1.0, (dx * dx + dy * dy) ** 0.5 / 1.4142)
        # Area dominates; centrality only separates same-size candidates, which
        # is why a large off-centre stamp still beats a small central speck.
        score = (area / frame_area) * (0.35 + 0.65 * centrality)
        if score > best_score:
            best_score, best, best_area = score, (x, y, w, h), area

    if report is not None:
        report.update(blobs=considered, edge_touching=touching,
                      mask_fraction=float((mask > 0).sum()) / frame_area,
                      chosen_fraction=best_area / frame_area if best else 0.0,
                      largest_rejected_fraction=largest_rejected / frame_area)

    if best is None:
        if report is not None:
            report["outcome"] = "all-touch-edge" if touching else "no-blobs"
        return None

    # Something bigger than the winner was thrown out for reaching the edge, so
    # the frame's real subject is the part that got rejected and the winner is
    # only whatever clutter sat fully inside (a pencil, a neighbouring stamp).
    # Handing that back is worse than declining, because it looks like an answer.
    #
    # This catches the narrower case than it might appear: an object covering a
    # stretch of border is flooded away as background by the seeds lying on it,
    # so it never becomes a rejected blob at all. What survives to be counted
    # here is an object that reaches the edge between seeds or stops within
    # edge_slack of it. Worth it anyway — over 150 frames the rule lifted the
    # 10th-percentile kept area from 0.21 to 0.26 for five more declines.
    if largest_rejected > best_area * tuning.reject_dominance:
        if report is not None:
            report["outcome"] = "rejected-dominates"
        return None

    if report is not None:
        report["outcome"] = "detected"

    x, y, w, h = best
    inv = 1.0 / scale
    # Negative padding is meaningful and allowed: it bites inside the detected
    # edge. The clamps below are one-sided maxima against the image bounds, so
    # they never widen an inset box back out.
    pad_x, pad_y = int(w * inv * tuning.pad), int(h * inv * tuning.pad)
    return (
        max(0, int(x * inv) - pad_x),
        max(0, int(y * inv) - pad_y),
        min(width, int((x + w) * inv) + pad_x),
        min(height, int((y + h) * inv) + pad_y),
    )


# ----------------------------------------------------------------------
# Applying a crop
# ----------------------------------------------------------------------

def crop_in_place(path: str, box: tuple[int, int, int, int]) -> None:
    """Crop `path` to `box` and write it back, snapshotting the original first.

    Destructive — the file is rewritten and the discarded pixels are gone from
    it. snapshot_original() is what makes that recoverable, and it is taken
    before the write and only once per image, so reverting returns the true
    original however many crops have been applied since.

    Raises on failure rather than reporting it, so a caller that can show the
    user an error does, and one that cannot logs it (see apply_to).
    """
    snapshot_original(path)
    with Image.open(path) as img:
        # `box` is in the EXIF-upright space every caller works in, so rotate
        # the pixels to match before cropping — Image.open() alone hands back
        # the raw sideways buffer, whose axes are swapped against the box.
        #
        # exif_transpose() also drops the now-satisfied Orientation tag. Baking
        # the rotation in is what keeps the result upright: the save writes no
        # EXIF, so a file that kept the raw pixels would lose the tag telling
        # every reader to rotate it and end up permanently on its side.
        upright = ImageOps.exif_transpose(img)
        upright.crop(box).save(path)


def apply_to(path: str, tuning: Tuning | None = None) -> bool:
    """Auto-crop `path` in place if a stamp is found. Returns whether it was.

    The unattended entry point: called as images arrive, where there is nobody
    to answer a dialog. It therefore never raises and never asks — a decline or
    a failure just leaves the file alone, and the Crop button is still there to
    do it by hand.
    """
    try:
        box = detect(path, tuning)
    except Exception as e:
        logger.warning(f"autocrop: detection failed for {path}: {e}")
        return False
    if box is None:
        logger.info(f"autocrop: declined {os.path.basename(path)} — left uncropped")
        return False

    try:
        crop_in_place(path, box)
    except Exception as e:
        logger.warning(f"autocrop: could not crop {path}: {e}")
        return False
    logger.info(f"autocrop: cropped {os.path.basename(path)} to {box}")
    return True
