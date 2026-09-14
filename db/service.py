"""
Database service layer for Stamp Identifier.
Provides clean CRUD operations for stamps, themes, and images.
"""
from datetime import datetime
import re
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.exc import IntegrityError
from db.models import (
    Stamp, StampImage, StampCopy, StampCopyOrigin, OriginLocation, Dealer,
    OriginPreset, Theme, VariantSet, PhysicalLocation, Series,
    stamp_theme_association,
)
from db.gallery_filters import FT, FIELDS_BY_KEY, build_clause
from logger import logger


class StampService:
    """Service for stamp database operations."""

    @staticmethod
    def create_stamp(session: Session, _commit: bool = True, **data) -> Stamp:
        """
        Create a new stamp in the database.

        Args:
            session: SQLAlchemy session
            **data: Stamp fields (title, scott_number, country, etc.)

        Returns:
            Created Stamp object

        Raises:
            ValueError: If stamp already exists (duplicate scott_number + country)
            RuntimeError: On database integrity errors
        """
        # Check for duplicates
        existing = (
            session.query(Stamp)
            .filter(
                Stamp.country == data["country"],
                Stamp.scott_number == data["scott_number"]
            )
            .first()
        )
        if existing:
            raise ValueError(
                f"Stamp {data['scott_number']} from {data['country']} already exists in database."
            )

        try:
            # Replace empty strings with None
            data = {key: (value if value != "" else None) for key, value in data.items()}

            # Clean up print_run to remove commas and non-numeric characters
            value = data.get("print_run")
            data["print_run"] = (
                None if value in (None, "", "None")
                else int(re.sub(r"[^\d]", "", str(value)))
            )

            data["issued_date"] = StampService._parse_date_string(data.get("issued_date"))
            data["expired_date"] = StampService._parse_date_string(data.get("expired_date"))

            stamp = Stamp(
                title=data["title"],
                scott_number=data["scott_number"],
                country=data["country"],
                series=data.get("series"),
                emission=data.get("emission"),
                face_value=data.get("face_value"),
                issued_date=data.get("issued_date"),
                expired_date=data.get("expired_date"),
                size=data.get("size"),
                perforation=data.get("perforation"),
                paper=data.get("paper"),
                gum=data.get("gum"),
                watermark=data.get("watermark"),
                printing=data.get("printing"),
                format=data.get("format"),
                print_run=data.get("print_run"),
                colors=data.get("colors"),
                designers=data.get("designers"),
                description=data.get("description"),
                variants=data.get("variants", False),
                owned=data.get("owned", True),
                variant_set_id=data.get("variant_set_id"),
                series_id=data.get("series_id"),
                physical_location_id=data.get("physical_location_id"),
            )

            # Attach themes
            for theme_name in data.get("themes", []):
                theme = session.query(Theme).filter_by(name=theme_name).one_or_none()
                if theme is None:
                    theme = Theme(name=theme_name)
                    session.add(theme)
                    session.flush()
                stamp.themes.append(theme)

            # Add image
            image_path = data.get("image_path")
            if image_path:
                stamp.images.append(StampImage(file_path=image_path))

            session.add(stamp)
            if _commit:
                session.commit()
                session.refresh(stamp)
            else:
                session.flush()  # assigns stamp.id without committing

            logger.info(f"Created stamp: {stamp.title} ({stamp.scott_number}, {stamp.country})")
            return stamp

        except IntegrityError as e:
            session.rollback()
            logger.error(f"Database integrity error: {e.orig}")
            raise RuntimeError(f"Database integrity error: {e.orig}") from e
        except Exception as e:
            session.rollback()
            logger.error(f"Error creating stamp: {e}")
            raise

    @staticmethod
    def _parse_date_string(value):
        if value is None:
            return None

        if isinstance(value, datetime):
            return value

        text = str(value).strip()
        if not text:
            return None

        # Try common date patterns
        patterns = [
            "%Y-%m-%d",
            "%Y/%m/%d",
            "%d-%m-%Y",
            "%d/%m/%Y",
            "%B %d, %Y",
            "%b %d, %Y",
            "%Y-%m",
            "%Y/%m",
            "%Y",
        ]

        for pattern in patterns:
            try:
                parsed = datetime.strptime(text, pattern)
                # For year-only values, normalize to Jan 1st of that year
                if pattern == "%Y":
                    parsed = parsed.replace(month=1, day=1)
                return parsed
            except ValueError:
                continue

        # Fallback: try ISO 8601 parse by replacing common separators
        try:
            parsed = datetime.fromisoformat(text)
            return parsed
        except Exception:
            pass

        logger.warning(f"Unable to parse date string '{text}', storing as None")
        return None

    @staticmethod
    def get_stamp(session: Session, scott_number: str, country: str) -> Stamp:
        """Get a stamp by Scott number and country."""
        return session.query(Stamp).filter_by(
            scott_number=scott_number,
            country=country
        ).first()

    @staticmethod
    def get_all_countries(session: Session) -> list[str]:
        """Get all distinct countries that have stamps."""
        results = (
            session.query(Stamp.country)
            .distinct()
            .order_by(Stamp.country)
            .all()
        )
        return [row[0] for row in results if row[0] is not None]

    @staticmethod
    def get_distinct_values(session: Session, field: str) -> list[str]:
        """Distinct non-empty values for a filter field, sorted, for its combo.

        Handles ENUM columns (distinct on the Stamp column) and the RELATION
        fields (distinct names from the related table, or conditions from
        stamp_copies). Returns [] for field types that use a free-text/number/
        date editor instead of a value dropdown.
        """
        spec = FIELDS_BY_KEY.get(field)
        if spec is None:
            return []

        if spec.type == FT.ENUM:
            col = getattr(Stamp, spec.key)
            rows = session.query(col).distinct().order_by(col).all()
        elif spec.type == FT.RELATION:
            model, attr = {
                "series":          (Series, Series.name),
                "themes":          (Theme, Theme.name),
                "location":        (PhysicalLocation, PhysicalLocation.name),
                "variant_set":     (VariantSet, VariantSet.name),
                "condition":       (StampCopy, StampCopy.condition),
                "origin_location": (OriginLocation, OriginLocation.name),
                "origin_dealer":   (Dealer, Dealer.name),
                "origin_method":   (StampCopyOrigin, StampCopyOrigin.method),
            }.get(spec.relation, (None, None))
            if attr is None:
                return []
            rows = session.query(attr).distinct().order_by(attr).all()
        else:
            return []

        return [r[0] for r in rows if r[0] not in (None, "")]

    @staticmethod
    def get_total_count(session: Session) -> int:
        """Return the total number of stamps in the database."""
        return session.query(Stamp).count()

    @staticmethod
    def get_country_count(session: Session) -> int:
        """Return the number of distinct countries represented in the database."""
        return session.query(Stamp.country).distinct().count()

    @staticmethod
    def get_stamps_by_country(session: Session, country: str) -> list[dict]:
        """Get all stamps in a country."""
        # selectinload, not lazy: the comprehension below reads s.images on every
        # row, which would otherwise emit one SELECT per stamp. This list can run
        # to hundreds of rows and is rebuilt on every save, so the N+1 is the
        # single most expensive thing the Database panel does. See the same
        # pattern on the other list queries in this module.
        stamps = (
            session.query(Stamp)
            .options(selectinload(Stamp.images))
            .filter_by(country=country)
            .all()
        )
        return [
            {
                "id": s.id,
                "title": s.title,
                "scott_number": s.scott_number,
                "series": s.series,
                "image_path": s.images[0].file_path if s.images else None,
            }
            for s in stamps
        ]

    @staticmethod
    def search_stamps(session: Session, query: str) -> list[Stamp]:
        """
        Search stamps by title, Scott number, or country.

        Args:
            session: SQLAlchemy session
            query: Search string

        Returns:
            List of matching stamps
        """
        q = f"%{query}%"
        return session.query(Stamp).filter(
            (Stamp.title.ilike(q)) |
            (Stamp.scott_number.ilike(q)) |
            (Stamp.country.ilike(q)) |
            (Stamp.series.ilike(q))
        ).all()

    @staticmethod
    def filter_by_tags(session: Session, tags: list[str]) -> list[Stamp]:
        """
        Filter stamps that have all specified tags/themes.

        Args:
            session: SQLAlchemy session
            tags: List of theme names

        Returns:
            List of stamps with all specified themes
        """
        return session.query(Stamp).filter(
            Stamp.themes.any(Theme.name.in_(tags))
        ).all()

    @staticmethod
    def get_gallery_stamps(
        session: Session,
        search: str = "",
        country: str = "",
        tags: list[str] | None = None,
        tag_mode: str = "OR",
        sort: str = "date",
        limit: int | None = None,
        offset: int = 0,
        rules: list[dict] | None = None,
        match: str = "AND",
    ) -> tuple[list[dict], int]:
        """Return (page of stamps, total matching count) for the gallery view.

        `rules` is the advanced filter-builder output — a list of rule dicts
        (see db.gallery_filters). They are combined with `match` ("AND" = match
        all, "OR" = match any) and AND-ed together with the quick-search /
        country / tag filters above, so an empty `rules` leaves behaviour
        identical to before.
        """
        q = session.query(Stamp)

        if search:
            like = f"%{search}%"
            q = q.filter(
                (Stamp.title.ilike(like)) |
                (Stamp.scott_number.ilike(like)) |
                (Stamp.country.ilike(like)) |
                (Stamp.series.ilike(like)) |
                (Stamp.themes.any(Theme.name.ilike(like)))
            )
        if country:
            q = q.filter(Stamp.country == country)
        if tags:
            if tag_mode == "AND":
                for tag in tags:
                    q = q.filter(Stamp.themes.any(Theme.name == tag))
            else:
                q = q.filter(Stamp.themes.any(Theme.name.in_(tags)))

        if rules:
            clauses = [c for r in rules if (c := build_clause(r)) is not None]
            if clauses:
                q = q.filter(and_(*clauses) if match == "AND" else or_(*clauses))

        order = {
            "date":    Stamp.added_to_db.desc(),
            "oldest":  Stamp.added_to_db,
            "scott":   Stamp.scott_number,
            "country": Stamp.country,
            "title":   Stamp.title,
        }.get(sort, Stamp.added_to_db.desc())
        q = q.order_by(order)

        total = q.count()
        if limit is not None:
            q = q.offset(offset).limit(limit)

        # Eager-load images after the count and the limit: counting a query with
        # a selectinload is wasted work, and the extra SELECT should cover only
        # the page being returned, not every matching row.
        q = q.options(selectinload(Stamp.images))

        return [
            {
                "id":           s.id,
                "title":        s.title,
                "scott_number": s.scott_number,
                "country":      s.country,
                "image_path":   s.images[0].file_path if s.images else None,
            }
            for s in q.all()
        ], total

    @staticmethod
    def update_stamp(session: Session, stamp_id: int, _commit: bool = True, **data) -> Stamp:
        """Update an existing stamp."""
        stamp = session.query(Stamp).get(stamp_id)
        if not stamp:
            raise ValueError(f"Stamp {stamp_id} not found")

        # Replace empty strings with None
        data = {key: (value if value != "" else None) for key, value in data.items()}

        try:
            for key, value in data.items():
                if key == "themes":
                    stamp.themes = []
                    for theme_name in value:
                        theme_name = theme_name.strip()
                        if not theme_name:
                            continue
                        theme = session.query(Theme).filter_by(name=theme_name).one_or_none()
                        if theme is None:
                            theme = Theme(name=theme_name)
                            session.add(theme)
                        stamp.themes.append(theme)
                elif key == "print_run":
                    if value in (None, "None"):
                        setattr(stamp, key, None)
                    else:
                        try:
                            setattr(stamp, key, int(re.sub(r"[^\d]", "", str(value) or "")) or None)
                        except ValueError:
                            setattr(stamp, key, None)
                elif key in ("issued_date", "expired_date"):
                    setattr(stamp, key, StampService._parse_date_string(value))
                elif key == "image_path":
                    # Images live in the StampImage relationship, not as a column
                    # on Stamp, so they'd otherwise be dropped by the hasattr
                    # branch below. A truthy path replaces the stamp's primary
                    # image; a falsy one leaves the existing image untouched
                    # (ordinary edits pass the unchanged path, making this a
                    # no-op unless the image was actually reassigned).
                    if value:
                        if stamp.images:
                            stamp.images[0].file_path = value
                        else:
                            stamp.images.append(StampImage(file_path=value))
                elif hasattr(stamp, key) and key != "id":
                    setattr(stamp, key, value)

            stamp.last_updated = datetime.utcnow()
            if _commit:
                session.commit()
            else:
                session.flush()
            logger.info(f"Updated stamp: {stamp.title}")
            return stamp
        except IntegrityError as e:
            session.rollback()
            logger.error(f"Database integrity error updating stamp: {e.orig}")
            raise RuntimeError(f"Database integrity error: {e.orig}") from e
        except Exception as e:
            session.rollback()
            logger.error(f"Error updating stamp: {e}")
            raise

    @staticmethod
    def delete_stamp(session: Session, stamp_id: int) -> bool:
        """Delete a stamp by ID."""
        stamp = session.query(Stamp).get(stamp_id)
        if not stamp:
            return False
        session.delete(stamp)
        session.commit()
        logger.info(f"Deleted stamp: {stamp.title}")
        return True


class ThemeService:
    """Service for theme database operations."""

    @staticmethod
    def get_all_themes(session: Session) -> list[Theme]:
        """Get all themes, sorted by name."""
        return session.query(Theme).order_by(Theme.name).all()

    @staticmethod
    def get_themes_by_recent_use(session: Session) -> list[Theme]:
        """Get all themes ordered by the most recent stamp they were applied to."""
        from sqlalchemy import func
        return (
            session.query(Theme)
            .outerjoin(stamp_theme_association, Theme.id == stamp_theme_association.c.theme_id)
            .outerjoin(Stamp, Stamp.id == stamp_theme_association.c.stamp_id)
            .group_by(Theme.id)
            .order_by(func.max(Stamp.added_to_db).desc().nulls_last(), Theme.name)
            .all()
        )

    @staticmethod
    def create_or_get_theme(session: Session, name: str) -> Theme:
        """Create theme if it doesn't exist, otherwise return existing."""
        theme = session.query(Theme).filter_by(name=name).one_or_none()
        if not theme:
            theme = Theme(name=name)
            session.add(theme)
            session.commit()
            logger.info(f"Created theme: {name}")
        return theme

    @staticmethod
    def rename_theme(session: Session, old_name: str, new_name: str) -> bool:
        """Rename a theme. Returns False if old_name not found or new_name already exists."""
        theme = session.query(Theme).filter_by(name=old_name).one_or_none()
        if not theme:
            return False
        if session.query(Theme).filter_by(name=new_name).one_or_none():
            return False
        theme.name = new_name
        session.commit()
        logger.info(f"Renamed theme: '{old_name}' → '{new_name}'")
        return True

    @staticmethod
    def delete_theme(session: Session, theme_id: int) -> bool:
        """Delete a theme."""
        theme = session.query(Theme).get(theme_id)
        if not theme:
            return False
        session.delete(theme)
        session.commit()
        logger.info(f"Deleted theme: {theme.name}")
        return True


class ImageService:
    """Service for stamp image operations."""

    @staticmethod
    def add_image(session: Session, stamp_id: int, file_path: str) -> StampImage:
        """Add image to a stamp, moving it out of the incoming folder if needed."""
        from image_storage import associate_image, deassociate_image

        stamp = session.query(Stamp).get(stamp_id)
        if not stamp:
            raise ValueError(f"Stamp {stamp_id} not found")

        # Associating a capture moves it from INCOMING_DIR to IMAGE_DIR; store the
        # final path. Roll the move back if the commit fails.
        stored_path = associate_image(file_path)
        image = StampImage(stamp_id=stamp_id, file_path=stored_path)
        session.add(image)
        try:
            session.commit()
        except Exception:
            session.rollback()
            if stored_path != file_path:
                deassociate_image(stored_path)
            raise
        logger.info(f"Added image to stamp {stamp_id}: {stored_path}")
        return image

    @staticmethod
    def get_stamp_images(session: Session, stamp_id: int) -> list[StampImage]:
        """Get all images for a stamp."""
        return session.query(StampImage).filter_by(stamp_id=stamp_id).all()

    @staticmethod
    def get_visual_candidates(
        session: Session,
        country: str | None = None,
        face_value: str | float | None = None,
    ) -> list[dict]:
        """Images eligible for a visual search, narrowed by country and value.

        Returns dicts of stamp_id / title / scott_number / country / face_value
        / file_path, one per image, ready to be shown as a result row.

        Both filters are optional and independent; passing neither returns the
        whole collection, which is a valid but slow search (see visual_search).

        Country is filtered in SQL, value is not. Face values are stored in
        Colnect's canonical form ("3 ¢ - United States cent") while the field
        holds a bare number, so matching needs parse_face_value rather than a
        LIKE — and country has already cut the row count to at most a few
        hundred by then, which makes the Python pass free in practice.

        Stamps whose value cannot be parsed (genuine no-face-value issues) are
        excluded only when a value filter is actually supplied; they should not
        vanish from an unfiltered search.
        """
        from helper_utils import parse_face_value

        q = session.query(
            StampImage.file_path,
            Stamp.id, Stamp.title, Stamp.scott_number, Stamp.country, Stamp.face_value,
        ).join(Stamp, StampImage.stamp_id == Stamp.id)

        if country:
            q = q.filter(Stamp.country == country)

        target = parse_face_value(face_value) if isinstance(face_value, str) else face_value
        rows = q.all()

        out = []
        for path, sid, title, scott, ctry, fv in rows:
            if target is not None:
                parsed = parse_face_value(fv)
                if parsed is None or abs(parsed - target) > 1e-9:
                    continue
            out.append({
                "stamp_id": sid, "title": title, "scott_number": scott,
                "country": ctry, "face_value": fv, "file_path": path,
            })
        return out

    @staticmethod
    def reassign_image(session: Session, file_path: str, from_stamp_id: int, to_stamp_id: int) -> bool:
        """Move a StampImage record from one stamp to another."""
        img = session.query(StampImage).filter_by(
            file_path=file_path, stamp_id=from_stamp_id
        ).one_or_none()
        if not img:
            return False
        img.stamp_id = to_stamp_id
        session.commit()
        logger.info(f"Reassigned image '{file_path}' from stamp {from_stamp_id} to stamp {to_stamp_id}")
        return True


class SeriesService:
    """Service for series operations."""

    @staticmethod
    def get_or_create_by_url(session: Session, name: str, url: str, _commit: bool = True) -> "Series":
        """Find a Series by URL (most reliable key) or create one."""
        existing = session.query(Series).filter_by(series_url=url).one_or_none()
        if existing:
            return existing
        s = Series(name=name, series_url=url)
        session.add(s)
        if _commit:
            session.commit()
        else:
            session.flush()
        logger.info(f"Created series: '{name}' ({url})")
        return s

    @staticmethod
    def get_or_create_by_name(session: Session, name: str, _commit: bool = True) -> "Series":
        """Find a Series by name (case-insensitive) or create one. Used when no series_url is available."""
        existing = session.query(Series).filter(Series.name.ilike(name)).one_or_none()
        if existing:
            return existing
        s = Series(name=name)
        session.add(s)
        if _commit:
            session.commit()
        else:
            session.flush()
        logger.info(f"Created series: '{name}' (no URL)")
        return s

    @staticmethod
    def get_by_id(session: Session, series_id: int) -> "Series | None":
        return session.query(Series).get(series_id)

    @staticmethod
    def count_stamps(session: Session, series_id: int) -> int:
        """Number of stamps saved in the database for this series."""
        return session.query(Stamp).filter_by(series_id=series_id).count()

    @staticmethod
    def get_all_with_country(session: Session) -> list[dict]:
        """Return all series with their stamp count and most-common country."""
        from sqlalchemy import func
        rows = (
            session.query(
                Series,
                func.count(Stamp.id).label("stamp_count"),
                func.max(Stamp.country).label("country"),
            )
            .outerjoin(Stamp, Stamp.series_id == Series.id)
            .group_by(Series.id)
            .order_by(Series.name)
            .all()
        )
        return [
            {
                "id":          r.Series.id,
                "name":        r.Series.name,
                "series_url":  r.Series.series_url or "",
                "country":     r.country or "",
                "stamp_count": r.stamp_count or 0,
            }
            for r in rows
        ]

    @staticmethod
    def get_stamps(session: Session, series_id: int) -> list[dict]:
        stamps = (
            session.query(Stamp)
            .options(selectinload(Stamp.images))
            .filter_by(series_id=series_id)
            .order_by(Stamp.scott_number)
            .all()
        )
        return [
            {
                "id":           s.id,
                "title":        s.title,
                "scott_number": s.scott_number,
                "country":      s.country,
                "image_path":   s.images[0].file_path if s.images else None,
            }
            for s in stamps
        ]

    @staticmethod
    def update(session: Session, series_id: int, _commit: bool = True, **data) -> "Series":
        s = session.query(Series).get(series_id)
        if not s:
            raise ValueError(f"Series {series_id} not found.")
        for key, value in data.items():
            if hasattr(s, key):
                setattr(s, key, value or None)
        if _commit:
            session.commit()
        else:
            session.flush()
        return s


class StampCopyService:
    """Service for tracking per-stamp copy counts and conditions."""

    CONDITIONS = [
        "Mint Never Hinged (MNH)",
        "Mint Hinged (MH)",
        "Mint / Unused",
        "No Gum No Cancel",
        "Fine Used (FU)",
        "Used",
        "CTO (Cancelled to Order)",
        "Faulty",
        "Other",
    ]

    @staticmethod
    def get_for_stamp(session: Session, stamp_id: int) -> list[StampCopy]:
        return (
            session.query(StampCopy)
            .filter_by(stamp_id=stamp_id)
            .order_by(StampCopy.id)
            .all()
        )

    @staticmethod
    def set_copies(session: Session, stamp_id: int, copies: list[dict], _commit: bool = True) -> None:
        """Replace all copy records for a stamp with the provided list.

        Each copy dict may carry an "origin" sub-dict (location/dealer names,
        method, price, date, notes). Since copy rows are deleted and rebuilt on
        every save, the origin travels in the dict and is rebuilt alongside the
        copy so provenance survives an edit.
        """
        session.query(StampCopy).filter_by(stamp_id=stamp_id).delete()
        for c in copies:
            condition = (c.get("condition") or "").strip()
            quantity  = int(c.get("quantity") or 1)
            if not (condition and quantity > 0):
                continue
            copy = StampCopy(
                stamp_id=stamp_id,
                condition=condition,
                quantity=quantity,
                notes=c.get("notes") or None,
            )
            origin = OriginService.build_origin(session, c.get("origin"))
            if origin is not None:
                copy.origin = origin
            session.add(copy)
        if _commit:
            session.commit()
        else:
            session.flush()


class OriginPresetService:
    """Named, reusable acquisition origins applied to individual copy rows.

    Presets are templates: to_origin() hands back a plain dict the copy row
    copies in, and nothing stays linked afterwards. Editing or deleting a preset
    never touches a stamp that was already saved with it.

    Separate from the batch defaults (ui/field_defaults.py), which pre-fill one
    origin into a new stamp's first row behind a global toggle.
    """

    # The origin-dict keys a preset carries, mapped to their column names. The
    # dict shape is OriginDialog's, so a preset round-trips through the form.
    FIELDS = {
        "location":      "location",
        "dealer":        "dealer",
        "method":        "method",
        "price":         "price",
        "acquired_date": "acquired_date",
        "notes":         "notes",
    }

    @staticmethod
    def get_all(session: Session) -> list[OriginPreset]:
        return session.query(OriginPreset).order_by(OriginPreset.name).all()

    @staticmethod
    def to_origin(preset: OriginPreset) -> dict:
        """The preset as an origin dict, dropping empties so an unset field
        falls through to whatever the row already had."""
        return {
            key: (getattr(preset, col) or "")
            for key, col in OriginPresetService.FIELDS.items()
            if (getattr(preset, col) or "").strip()
        }

    @staticmethod
    def save(session: Session, name: str, origin: dict) -> OriginPreset:
        """Create a preset, or overwrite the one already using this name."""
        name = (name or "").strip()
        if not name:
            raise ValueError("Preset name cannot be empty.")

        preset = session.query(OriginPreset).filter_by(name=name).one_or_none()
        if preset is None:
            preset = OriginPreset(name=name)
            session.add(preset)
        for key, col in OriginPresetService.FIELDS.items():
            setattr(preset, col, (str(origin.get(key) or "").strip()) or None)
        session.commit()
        logger.info(f"Saved origin preset: {name}")
        return preset

    @staticmethod
    def rename(session: Session, preset_id: int, new_name: str) -> OriginPreset:
        new_name = (new_name or "").strip()
        if not new_name:
            raise ValueError("Preset name cannot be empty.")
        clash = session.query(OriginPreset).filter_by(name=new_name).one_or_none()
        if clash and clash.id != preset_id:
            raise ValueError(f"An origin preset named '{new_name}' already exists.")
        preset = session.get(OriginPreset, preset_id)
        if not preset:
            raise ValueError("Origin preset not found.")
        preset.name = new_name
        session.commit()
        logger.info(f"Renamed origin preset {preset_id} -> '{new_name}'")
        return preset

    @staticmethod
    def delete(session: Session, preset_id: int) -> bool:
        preset = session.get(OriginPreset, preset_id)
        if not preset:
            return False
        name = preset.name
        session.delete(preset)
        session.commit()
        logger.info(f"Deleted origin preset: {name}")
        return True


class OriginService:
    """Acquisition provenance: reusable locations/dealers and per-copy origins."""

    # Suggested acquisition methods; the UI offers these but stores free text.
    METHODS = ["Bought", "Given", "Traded", "Found", "Inherited", "Other"]

    @staticmethod
    def get_all_locations(session: Session) -> list[OriginLocation]:
        return session.query(OriginLocation).order_by(OriginLocation.name).all()

    @staticmethod
    def get_all_dealers(session: Session) -> list[Dealer]:
        return session.query(Dealer).order_by(Dealer.name).all()

    @staticmethod
    def get_location_date(session: Session, name: str) -> str | None:
        """The most recent acquisition date recorded at this location, as
        'YYYY-MM-DD', or None. Used to auto-fill the date when a location is
        picked (locations recur, so this is the last date seen there, not a
        fixed property of the place)."""
        name = (name or "").strip()
        if not name:
            return None
        row = (
            session.query(StampCopyOrigin.acquired_date)
            .join(OriginLocation, StampCopyOrigin.location_id == OriginLocation.id)
            .filter(
                OriginLocation.name == name,
                StampCopyOrigin.acquired_date.isnot(None),
            )
            .order_by(StampCopyOrigin.acquired_date.desc())
            .first()
        )
        return row[0].strftime("%Y-%m-%d") if row and row[0] else None

    @staticmethod
    def get_method_suggestions(session: Session) -> list[str]:
        """Suggested methods first (Bought/Given/…), then any previously entered
        custom methods not already in that list, so past entries are reusable."""
        rows = (
            session.query(StampCopyOrigin.method)
            .filter(StampCopyOrigin.method.isnot(None))
            .distinct()
            .all()
        )
        seen = {m.lower() for m in OriginService.METHODS}
        extra = sorted(
            r[0] for r in rows if r[0] and r[0].lower() not in seen
        )
        return OriginService.METHODS + extra

    @staticmethod
    def get_or_create_location(session: Session, name: str) -> OriginLocation | None:
        name = (name or "").strip()
        if not name:
            return None
        loc = session.query(OriginLocation).filter_by(name=name).one_or_none()
        if loc is None:
            loc = OriginLocation(name=name)
            session.add(loc)
            session.flush()
        return loc

    @staticmethod
    def get_or_create_dealer(session: Session, name: str) -> Dealer | None:
        name = (name or "").strip()
        if not name:
            return None
        dealer = session.query(Dealer).filter_by(name=name).one_or_none()
        if dealer is None:
            dealer = Dealer(name=name)
            session.add(dealer)
            session.flush()
        return dealer

    @staticmethod
    def _parse_price(value):
        if value in (None, ""):
            return None
        try:
            return round(float(re.sub(r"[^\d.\-]", "", str(value))), 2)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def build_origin(session: Session, data: dict | None) -> StampCopyOrigin | None:
        """Build a StampCopyOrigin from a UI dict, or None if it holds nothing.

        location/dealer arrive as names; blanks become None. Locations and
        dealers are get-or-created so they're reusable across copies.
        """
        if not data:
            return None
        location = OriginService.get_or_create_location(session, data.get("location"))
        dealer   = OriginService.get_or_create_dealer(session, data.get("dealer"))
        method   = (data.get("method") or "").strip() or None
        price    = OriginService._parse_price(data.get("price"))
        acquired = StampService._parse_date_string(data.get("acquired_date"))
        notes    = (data.get("notes") or "").strip() or None

        # Nothing meaningful entered → no origin row.
        if not any([location, dealer, method, price is not None, acquired, notes]):
            return None

        return StampCopyOrigin(
            location=location,
            dealer=dealer,
            method=method,
            price=price,
            acquired_date=acquired,
            notes=notes,
        )

    @staticmethod
    def origin_to_dict(origin: StampCopyOrigin | None) -> dict:
        """Flatten an origin (with related names) for the UI. Empty dict if None."""
        if origin is None:
            return {}
        return {
            "location":      origin.location.name if origin.location else "",
            "dealer":        origin.dealer.name if origin.dealer else "",
            "method":        origin.method or "",
            "price":         "" if origin.price is None else f"{origin.price:.2f}",
            "acquired_date": origin.acquired_date.strftime("%Y-%m-%d") if origin.acquired_date else "",
            "notes":         origin.notes or "",
        }


class PhysicalLocationService:
    """Service for physical location operations."""

    @staticmethod
    def get_all(session: Session) -> list[PhysicalLocation]:
        return session.query(PhysicalLocation).order_by(PhysicalLocation.name).all()

    @staticmethod
    def get_all_with_count(session: Session) -> list[dict]:
        """Return all locations with their stamp count, sorted by name."""
        from sqlalchemy import func
        rows = (
            session.query(
                PhysicalLocation,
                func.count(Stamp.id).label("stamp_count"),
            )
            .outerjoin(Stamp, Stamp.physical_location_id == PhysicalLocation.id)
            .group_by(PhysicalLocation.id)
            .order_by(PhysicalLocation.name)
            .all()
        )
        return [
            {"id": r.PhysicalLocation.id, "name": r.PhysicalLocation.name,
             "stamp_count": r.stamp_count or 0}
            for r in rows
        ]

    @staticmethod
    def get_stamps(session: Session, location_id: int) -> list[dict]:
        stamps = (
            session.query(Stamp)
            .options(selectinload(Stamp.images))
            .filter_by(physical_location_id=location_id)
            .order_by(Stamp.country, Stamp.scott_number)
            .all()
        )
        return [
            {"id": s.id, "title": s.title, "scott_number": s.scott_number,
             "country": s.country, "series": s.series,
             "image_path": s.images[0].file_path if s.images else None}
            for s in stamps
        ]

    @staticmethod
    def create(session: Session, name: str) -> PhysicalLocation:
        name = name.strip()
        if not name:
            raise ValueError("Location name cannot be empty.")
        if session.query(PhysicalLocation).filter_by(name=name).one_or_none():
            raise ValueError(f"Location '{name}' already exists.")
        loc = PhysicalLocation(name=name)
        session.add(loc)
        session.commit()
        logger.info(f"Created physical location: {name}")
        return loc

    @staticmethod
    def delete(session: Session, location_id: int) -> bool:
        loc = session.query(PhysicalLocation).get(location_id)
        if not loc:
            return False
        session.delete(loc)
        session.commit()
        logger.info(f"Deleted physical location: {loc.name}")
        return True


class VariantSetService:
    """Service for variant set operations."""

    @staticmethod
    def get_all(session: Session) -> list[VariantSet]:
        return session.query(VariantSet).order_by(VariantSet.name).all()

    @staticmethod
    def create(session: Session, name: str) -> VariantSet:
        if session.query(VariantSet).filter_by(name=name).one_or_none():
            raise ValueError(f"Variant set '{name}' already exists.")
        vs = VariantSet(name=name)
        session.add(vs)
        session.commit()
        logger.info(f"Created variant set: {name}")
        return vs

    @staticmethod
    def rename(session: Session, variant_set_id: int, new_name: str) -> VariantSet:
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("Name cannot be empty.")
        if session.query(VariantSet).filter_by(name=new_name).one_or_none():
            raise ValueError(f"A variant set named '{new_name}' already exists.")
        vs = session.query(VariantSet).get(variant_set_id)
        if not vs:
            raise ValueError("Variant set not found.")
        vs.name = new_name
        session.commit()
        logger.info(f"Renamed variant set {variant_set_id} → '{new_name}'")
        return vs

    @staticmethod
    def save_notes(session: Session, variant_set_id: int, notes: str) -> None:
        vs = session.get(VariantSet, variant_set_id)
        if vs:
            vs.notes = notes.strip() or None
            session.commit()

    @staticmethod
    def get_series_usage(
        session: Session, series_id: int, exclude_stamp_id: int | None = None
    ) -> dict:
        """Summarise which variant sets the other stamps in a series use.

        Returns a dict with:
          sets     - [(id, name, count), ...] ordered most-used first, one entry
                     per distinct variant set in use across the series
          without  - how many stamps in the series have no variant set at all
          total    - stamps considered (excluding exclude_stamp_id)

        Stamps with no variant set are counted in `without` but never block a
        consensus; callers decide what to do when sets has exactly one entry.
        """
        q = session.query(Stamp.variant_set_id).filter(Stamp.series_id == series_id)
        if exclude_stamp_id is not None:
            q = q.filter(Stamp.id != exclude_stamp_id)
        ids = [row[0] for row in q.all()]

        without = sum(1 for i in ids if i is None)
        counts: dict[int, int] = {}
        for i in ids:
            if i is not None:
                counts[i] = counts.get(i, 0) + 1

        sets: list[tuple[int, str, int]] = []
        if counts:
            rows = (
                session.query(VariantSet)
                .filter(VariantSet.id.in_(list(counts.keys())))
                .all()
            )
            names = {vs.id: vs.name for vs in rows}
            sets = sorted(
                ((i, names.get(i, f"#{i}"), c) for i, c in counts.items()),
                key=lambda t: (-t[2], t[1].lower()),
            )

        return {"sets": sets, "without": without, "total": len(ids)}

    @staticmethod
    def get_stamps(session: Session, variant_set_id: int) -> list[dict]:
        stamps = (
            session.query(Stamp)
            .options(selectinload(Stamp.images))
            .filter_by(variant_set_id=variant_set_id)
            .all()
        )
        return [
            {
                "id":           s.id,
                "title":        s.title,
                "scott_number": s.scott_number,
                "country":      s.country,
                "image_path":   s.images[0].file_path if s.images else None,
            }
            for s in stamps
        ]
