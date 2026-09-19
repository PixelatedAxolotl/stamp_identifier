"""
Mobile API for Stamp Collection.
Run from project root: uvicorn stamp_phone.api:app --host 0.0.0.0 --port 8080 --reload
"""
import asyncio
import sys
import uuid
from pathlib import Path
from datetime import datetime
from typing import Optional, Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, HTTPException, Depends, Query, File, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session, subqueryload

from db.session import SessionLocal
from db.models import Stamp, StampImage, Theme
from db.service import (
    StampService, ThemeService, VariantSetService,
    PhysicalLocationService, SeriesService, StampCopyService,
)
from config import IMAGE_DIR, INCOMING_DIR, AUTOCROP_ON_ARRIVAL
import autocrop

# No CORS middleware: the PWA is served by this same app from the StaticFiles mount
# below, and app.js uses window.location.origin, so every request is same-origin.
# allow_origins=["*"] would let any site the user visits read these responses.
app = FastAPI(title="Stamp Collection Mobile API")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- Serialisers ---

def _image_filename(file_path: str | None) -> str | None:
    return Path(file_path).name if file_path else None


def _stamp_summary(s: Stamp) -> dict:
    return {
        "id": s.id,
        "title": s.title,
        "scott_number": s.scott_number,
        "country": s.country,
        "image_filename": _image_filename(s.images[0].file_path) if s.images else None,
        "conditions": [c.condition for c in s.copies],
    }


def _stamp_detail(s: Stamp) -> dict:
    return {
        "id": s.id,
        "title": s.title,
        "scott_number": s.scott_number,
        "country": s.country,
        "series": s.series,
        "series_id": s.series_id,
        "series_name": s.series_obj.name if s.series_obj else None,
        "issued_date": s.issued_date.isoformat() if s.issued_date else None,
        "expired_date": s.expired_date.isoformat() if s.expired_date else None,
        "size": s.size,
        "colors": s.colors,
        "designers": s.designers,
        "format": s.format,
        "emission": s.emission,
        "paper": s.paper,
        "gum": s.gum,
        "perforation": s.perforation,
        "printing": s.printing,
        "face_value": s.face_value,
        "print_run": s.print_run,
        "watermark": s.watermark,
        "description": s.description,
        "variants": s.variants,
        "owned": s.owned,
        "variant_set_id": s.variant_set_id,
        "variant_set_name": s.variant_set.name if s.variant_set else None,
        "physical_location_id": s.physical_location_id,
        "physical_location_name": s.physical_location.name if s.physical_location else None,
        # sorted() because the relationship declares no ordering: the order here
        # is otherwise an artefact of the loading strategy, and switching
        # sync_full_data to eager loading silently reshuffled it.
        "themes": sorted(t.name for t in s.themes),
        "copies": [
            {"id": c.id, "condition": c.condition, "quantity": c.quantity, "notes": c.notes}
            for c in s.copies
        ],
        "images": [_image_filename(img.file_path) for img in s.images if img.file_path],
        "added_to_db": s.added_to_db.isoformat() if s.added_to_db else None,
        "last_updated": s.last_updated.isoformat() if s.last_updated else None,
    }


# --- API routes ---

@app.get("/api/stamps")
def list_stamps(
    search: str = "",
    country: str = "",
    theme: str = "",
    owned: Optional[bool] = None,
    sort: str = "date",
    limit: int = Query(default=50, le=200),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    q = db.query(Stamp)
    if search:
        like = f"%{search}%"
        q = q.filter(
            (Stamp.title.ilike(like)) |
            (Stamp.scott_number.ilike(like)) |
            (Stamp.country.ilike(like)) |
            (Stamp.series.ilike(like))
        )
    if country:
        q = q.filter(Stamp.country == country)
    if theme:
        q = q.filter(Stamp.themes.any(Theme.name == theme))
    if owned is not None:
        q = q.filter(Stamp.owned == owned)

    order_map = {
        "date":    Stamp.added_to_db.desc(),
        "oldest":  Stamp.added_to_db.asc(),
        "scott":   Stamp.scott_number.asc(),
        "country": Stamp.country.asc(),
        "title":   Stamp.title.asc(),
    }
    q = q.order_by(order_map.get(sort, Stamp.added_to_db.desc()))

    total = q.count()
    stamps = (
        q.options(subqueryload(Stamp.images), subqueryload(Stamp.copies))
        .offset(offset).limit(limit).all()
    )
    return {
        "stamps": [_stamp_summary(s) for s in stamps],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@app.get("/api/stamps/{stamp_id}")
def get_stamp(stamp_id: int, db: Session = Depends(get_db)):
    s = db.query(Stamp).get(stamp_id)
    if not s:
        raise HTTPException(status_code=404, detail="Stamp not found")
    return _stamp_detail(s)


@app.get("/api/filters")
def get_filters(db: Session = Depends(get_db)):
    return {
        "countries": StampService.get_all_countries(db),
        "themes": [t.name for t in ThemeService.get_all_themes(db)],
        "series": [{"id": r["id"], "name": r["name"]} for r in SeriesService.get_all_with_country(db)],
        "physical_locations": [{"id": l.id, "name": l.name} for l in PhysicalLocationService.get_all(db)],
        "variant_sets": [{"id": vs.id, "name": vs.name} for vs in VariantSetService.get_all(db)],
        "conditions": StampCopyService.CONDITIONS,
    }


@app.get("/api/images/{filename}")
def serve_image(filename: str):
    # Reject anything that does not land inside IMAGE_DIR. Checking for "/", "\\" and
    # ".." is not sufficient on Windows: pathlib discards the left-hand path when the
    # right side carries a drive letter, so Path("storage/images") / "C:.env" is just
    # "C:.env" -- drive-relative, i.e. resolved against the server's working directory.
    # .name strips any directory part; resolve() + is_relative_to() catches the rest.
    base = Path(IMAGE_DIR).resolve()
    path = (base / Path(filename).name).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(str(path))


@app.get("/api/sync/manifest")
def sync_manifest(db: Session = Depends(get_db)):
    rows = db.query(Stamp.id, Stamp.last_updated, Stamp.added_to_db).all()
    return [
        {
            "id": r.id,
            "modified": (r.last_updated or r.added_to_db).isoformat()
            if (r.last_updated or r.added_to_db) else None,
        }
        for r in rows
    ]


@app.get("/api/sync/data")
def sync_full_data(db: Session = Depends(get_db)):
    # Eager-load every relationship _stamp_detail touches. Without this each stamp
    # costs six extra round trips, which at 2900 stamps is ~5s of pure N+1 -- long
    # enough that the phone's fetch can give up and the service worker reports a
    # synthetic 503 (sw.js). Same treatment as list_stamps above.
    stamps = (
        db.query(Stamp)
        .options(
            subqueryload(Stamp.images),
            subqueryload(Stamp.copies),
            subqueryload(Stamp.themes),
            subqueryload(Stamp.series_obj),
            subqueryload(Stamp.variant_set),
            subqueryload(Stamp.physical_location),
        )
        .all()
    )
    return {
        "stamps": [_stamp_detail(s) for s in stamps],
        "themes": [{"id": t.id, "name": t.name} for t in ThemeService.get_all_themes(db)],
        "series": SeriesService.get_all_with_country(db),
        "variant_sets": [
            {"id": vs.id, "name": vs.name, "notes": vs.notes}
            for vs in VariantSetService.get_all(db)
        ],
        "physical_locations": [
            {"id": l.id, "name": l.name} for l in PhysicalLocationService.get_all(db)
        ],
        "exported_at": datetime.utcnow().isoformat(),
    }


@app.get("/api/sync/images")
def sync_image_list(db: Session = Depends(get_db)):
    images = db.query(StampImage).all()
    result = []
    for img in images:
        filename = _image_filename(img.file_path)
        if filename:
            p = Path(IMAGE_DIR) / filename
            result.append({
                "stamp_id": img.stamp_id,
                "filename": filename,
                "size": p.stat().st_size if p.exists() else 0,
            })
    return result


class PatchStampRequest(BaseModel):
    title: Optional[str] = None
    scott_number: Optional[str] = None
    country: Optional[str] = None
    series: Optional[str] = None
    series_id: Optional[int] = None
    issued_date: Optional[str] = None
    expired_date: Optional[str] = None
    size: Optional[str] = None
    colors: Optional[str] = None
    designers: Optional[str] = None
    format: Optional[str] = None
    emission: Optional[str] = None
    paper: Optional[str] = None
    gum: Optional[str] = None
    perforation: Optional[str] = None
    printing: Optional[str] = None
    face_value: Optional[str] = None
    print_run: Optional[int] = None
    watermark: Optional[str] = None
    description: Optional[str] = None
    variants: Optional[bool] = None
    owned: Optional[bool] = None
    variant_set_id: Optional[int] = None
    physical_location_id: Optional[int] = None
    themes: Optional[list[str]] = None
    copies: Optional[list[dict]] = None


@app.patch("/api/stamps/{stamp_id}")
def update_stamp(stamp_id: int, body: PatchStampRequest, db: Session = Depends(get_db)):
    changes = body.model_dump(exclude_unset=True)
    copies = changes.pop("copies", None)
    if not changes and copies is None:
        raise HTTPException(status_code=400, detail="No fields to update")
    try:
        if changes:
            StampService.update_stamp(db, stamp_id, **changes)
        if copies is not None:
            StampCopyService.set_copies(db, stamp_id, copies)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    s = db.query(Stamp).get(stamp_id)
    db.refresh(s)
    return _stamp_detail(s)


class EditQueueItem(BaseModel):
    stamp_id: int
    changes: dict[str, Any]
    queued_at: str


@app.post("/api/sync/push")
def sync_push(edits: list[EditQueueItem], db: Session = Depends(get_db)):
    results = []
    for edit in edits:
        try:
            changes = dict(edit.changes)
            copies = changes.pop("copies", None)
            if changes:
                StampService.update_stamp(db, edit.stamp_id, **changes)
            if copies is not None:
                StampCopyService.set_copies(db, edit.stamp_id, copies)
            results.append({"stamp_id": edit.stamp_id, "status": "ok"})
        except Exception as e:
            results.append({"stamp_id": edit.stamp_id, "status": "error", "detail": str(e)})
    return {"results": results}


UPLOAD_MAX_BYTES = 40 * 1024 * 1024   # a 12MP iPhone JPEG is ~3MB; this is deliberately generous
_JPEG_MAGIC = b"\xff\xd8\xff"


@app.post("/api/upload")
async def upload_image(file: UploadFile = File(...)):
    """Accept a photo from the phone and drop it into INCOMING_DIR.

    The bytes are written verbatim — no re-encode, no resize — so the file on
    disk is byte-identical to what the phone's camera produced. Orientation is
    left in EXIF and applied at load time (see ui/panels/history.py) rather than
    baked in, which would cost a lossy generation.

    The filename is always generated here; the client's is never trusted. The
    extension must be .jpg because the desktop History panel only lists that
    suffix.
    """
    incoming = Path(INCOMING_DIR)
    incoming.mkdir(parents=True, exist_ok=True)

    final = incoming / f"phone_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}.jpg"
    # .part is deliberately not .jpg so neither the History listing nor its
    # folder watcher can pick the file up while it is still being written.
    part = final.with_suffix(".part")

    total = 0
    checked_magic = False
    try:
        with part.open("wb") as fh:
            while chunk := await file.read(1024 * 1024):
                if not checked_magic:
                    checked_magic = True
                    if not chunk.startswith(_JPEG_MAGIC):
                        raise HTTPException(
                            status_code=415,
                            detail="Not a JPEG (HEIC?). On the phone set "
                                   "Settings > Camera > Formats > Most Compatible.",
                        )
                total += len(chunk)
                if total > UPLOAD_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="File too large")
                fh.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="Empty upload")
        # Atomic publish — the History watcher must never observe a partial file.
        part.replace(final)
    except HTTPException:
        part.unlink(missing_ok=True)
        raise
    except Exception as e:
        part.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=str(e))

    # Auto-crop before responding, so the phone's "uploaded" means the image is
    # settled rather than about to change underneath the History strip. Detection
    # is ~0.4s of OpenCV on a 12MP frame, so it goes to a worker thread — running
    # it inline would stall every other request on this event loop for that long.
    # apply_to() never raises: a failed crop must not fail an upload that
    # otherwise succeeded, since the bytes are already safely on disk.
    cropped = False
    if AUTOCROP_ON_ARRIVAL:
        cropped = await asyncio.to_thread(autocrop.apply_to, str(final))

    return {"filename": final.name, "bytes": total, "cropped": cropped}


_APP_DIR = Path(__file__).parent / "app"

# sw.js and index.html are served with no-cache, ahead of the StaticFiles mount.
# Everything else in the shell is versioned by SHELL_CACHE inside the worker, but
# the worker script and the page that registers it are the two files that can
# invalidate that cache — so they must never come from a stale HTTP cache. Without
# this, StaticFiles sends no Cache-Control at all, Safari applies heuristic
# freshness, and an updated worker is simply never noticed.
_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


@app.get("/sw.js")
def service_worker():
    return FileResponse(_APP_DIR / "sw.js", media_type="application/javascript", headers=_NO_CACHE)


@app.get("/")
def index():
    return FileResponse(_APP_DIR / "index.html", media_type="text/html", headers=_NO_CACHE)


# Static files — must be mounted after all API routes
app.mount("/", StaticFiles(directory=str(_APP_DIR), html=True), name="static")
