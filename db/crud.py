from datetime import datetime
import re
from sqlalchemy.orm import Session
from db.models import Stamp, StampImage, Theme, stamp_theme_association

from sqlalchemy.exc import IntegrityError

def add_stamp(session, **data):
    # 1. Explicit existence check
    existing = (
        session.query(Stamp)
        .filter(
            Stamp.country == data["country"],
            Stamp.scott_number == data["scott_number"]
        )
        .first()
    )
    if existing:
        raise ValueError("This stamp already exists (country + Scott number).")

    try:
        # Replace empty strings with None
        data = {key: (value if value != "" else None) for key, value in data.items()}
        
        # clean up print_run to remove commas and non-numeric characters
        value = data.get("print_run")
        data["print_run"] = (
            None if value in (None, "", "None")
            else int(re.sub(r"[^\d]", "", str(value)))
        )

        stamp = Stamp(
            title=data["title"],
            scott_number=data["scott_number"],
            country=data["country"],
            series=data["series"],
            emission=data["emission"],
            face_value=data["face_value"],
            issued_date=data["issued_date"],
            expired_date=data["expired_date"],
            size=data["size"],
            perforation=data["perforation"],
            paper=data["paper"],
            gum=data["gum"],
            watermark=data["watermark"],
            printing=data["printing"],
            format=data["format"],
            print_run=data["print_run"],
            colors=data["colors"],
            designers=data["designers"],
            description=data["description"],
            variants=data.get("variants", False)
        )

        # Attach themes
        for theme_name in data.get("themes", []):
            # Try to get existing theme
            theme = session.query(Theme).filter_by(name=theme_name).one_or_none()
            if theme is None:
                # Create new theme if it doesn’t exist
                theme = Theme(name=theme_name)
                session.add(theme)
                session.flush()  # Ensure it gets an ID
            stamp.themes.append(theme)


        # images
        stamp.images.append(StampImage(file_path=data["image_path"]))

        session.add(stamp)
        session.commit()
        session.refresh(stamp)
        return stamp

    except IntegrityError as e:
        session.rollback()
        raise RuntimeError(f"Database integrity error: {e.orig}") from e


def get_stamp_by_scott(db: Session, scott_number: str, country: str):
    return db.query(Stamp).filter_by(scott_number=scott_number, country=country).first()

def get_all_countries(db: Session):
    """
    Returns a sorted list of distinct countries that have stamps.
    """
    results = (
        db.query(Stamp.country)
        .distinct()
        .order_by(Stamp.country)
        .all()
    )
    return [row[0] for row in results if row[0] is not None]


def get_stamps_by_country(db: Session, country: str):
    """
    Returns a list of dicts containing image path, title, and series
    for all stamps in the given country.
    """
    results = (
        db.query(
            Stamp.id,
            Stamp.title,
            Stamp.series,
            StampImage.file_path
        )
        .join(StampImage, Stamp.images)
        .filter(Stamp.country == country)
        .order_by(Stamp.title)
        .all()
    )

    return [
        {
            "id": stamp_id,
            "title": title,
            "series": series,
            "image_path": file_path,
        }
        for stamp_id, title, series, file_path in results
    ]



def update_stamp(db: Session, stamp: Stamp, **kwargs):
    for key, value in kwargs.items():
        if hasattr(stamp, key):
            setattr(stamp, key, value)
    stamp.last_updated = datetime.utcnow()
    db.commit()
    db.refresh(stamp)
    return stamp

def delete_stamp(db: Session, stamp: Stamp):
    db.delete(stamp)
    db.commit()
