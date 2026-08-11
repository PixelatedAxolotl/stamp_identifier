"""
Database service layer for Stamp Identifier.
Provides clean CRUD operations for stamps, themes, and images.
"""
from datetime import datetime
import re
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from db.models import Stamp, StampImage, StampCopy, Theme, VariantSet, PhysicalLocation, Series, stamp_theme_association
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
                "series":      (Series, Series.name),
                "themes":      (Theme, Theme.name),
                "location":    (PhysicalLocation, PhysicalLocation.name),
                "variant_set": (VariantSet, VariantSet.name),
                "condition":   (StampCopy, StampCopy.condition),
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
        stamps = session.query(Stamp).filter_by(country=country).all()
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
        """Replace all copy records for a stamp with the provided list."""
        session.query(StampCopy).filter_by(stamp_id=stamp_id).delete()
        for c in copies:
            condition = (c.get("condition") or "").strip()
            quantity  = int(c.get("quantity") or 1)
            if condition and quantity > 0:
                session.add(StampCopy(
                    stamp_id=stamp_id,
                    condition=condition,
                    quantity=quantity,
                    notes=c.get("notes") or None,
                ))
        if _commit:
            session.commit()
        else:
            session.flush()


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
    def get_stamps(session: Session, variant_set_id: int) -> list[dict]:
        stamps = session.query(Stamp).filter_by(variant_set_id=variant_set_id).all()
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
