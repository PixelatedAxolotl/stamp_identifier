from sqlalchemy import (
    Boolean,
    Column,
    Integer,
    Numeric,
    Table,
    Text,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy import Column, Integer, String
from .base import Base

#Base = declarative_base()

# Many-to-many bridge table for stamps and themes
stamp_theme_association = Table(
    "stamp_theme_association",
    Base.metadata,
    Column("stamp_id", Integer, ForeignKey("stamps.id"), primary_key=True),
    Column("theme_id", Integer, ForeignKey("themes.id"), primary_key=True),
)

class Series(Base):
    __tablename__ = "series"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    name            = Column(String, nullable=False)
    series_url      = Column(String, nullable=True, unique=True)
    series_complete = Column(String, nullable=True)
    comments        = Column(Text,   nullable=True)

    stamps = relationship("Stamp", back_populates="series_obj")


class PhysicalLocation(Base):
    __tablename__ = "physical_locations"

    id   = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, unique=True, nullable=False)

    stamps = relationship("Stamp", back_populates="physical_location")


class VariantSet(Base):
    __tablename__ = "variant_sets"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    name       = Column(String, nullable=False, unique=True)
    notes      = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    stamps = relationship("Stamp", back_populates="variant_set")


class Stamp(Base):
    __tablename__ = "stamps"
    __table_args__ = (UniqueConstraint("scott_number", "country", name="uq_scott_country"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String, nullable=False)
    scott_number = Column(String, nullable=False)
    country = Column(String, nullable=False)
    series = Column(String)
    issued_date = Column(DateTime, nullable=True)
    expired_date = Column(DateTime, nullable=True)
    size = Column(String)
    colors = Column(String)
    designers = Column(String)
    format = Column(String)
    emission = Column(String)
    paper = Column(String)
    gum = Column(String)
    perforation = Column(String)
    printing = Column(String)
    face_value = Column(String)
    print_run = Column(Integer)
    watermark = Column(String)
    description = Column(Text)
    variants = Column(Boolean, default=False)
    owned = Column(Boolean, default=True, nullable=False)
    variant_set_id = Column(Integer, ForeignKey("variant_sets.id", ondelete="SET NULL"), nullable=True)
    series_id            = Column(Integer, ForeignKey("series.id", ondelete="SET NULL"), nullable=True)
    physical_location_id = Column(Integer, ForeignKey("physical_locations.id", ondelete="SET NULL"), nullable=True)

    # Timestamps
    added_to_db = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_updated = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationship to themes
    themes = relationship(
        "Theme",
        secondary=stamp_theme_association,
        back_populates="stamps"
    )

    # Relationship to images
    images = relationship("StampImage", back_populates="stamp", cascade="all, delete-orphan")

    # Relationship to variant set
    variant_set = relationship("VariantSet", back_populates="stamps")

    # Relationship to series
    series_obj = relationship("Series", back_populates="stamps")

    # Relationship to physical location
    physical_location = relationship("PhysicalLocation", back_populates="stamps")

    # Relationship to copy records (quantity + condition)
    copies = relationship("StampCopy", back_populates="stamp", cascade="all, delete-orphan")

class StampCopy(Base):
    __tablename__ = "stamp_copies"

    id        = Column(Integer, primary_key=True, autoincrement=True)
    stamp_id  = Column(Integer, ForeignKey("stamps.id", ondelete="CASCADE"), nullable=False)
    condition = Column(String, nullable=False)
    quantity  = Column(Integer, nullable=False, default=1)
    notes     = Column(String, nullable=True)

    stamp = relationship("Stamp", back_populates="copies")

    # Where/how this copy (a batch acquired together) was obtained. One origin
    # per copy; split copies when they came from different places.
    origin = relationship(
        "StampCopyOrigin",
        back_populates="copy",
        uselist=False,
        cascade="all, delete-orphan",
    )


class OriginLocation(Base):
    """Where a copy was acquired, e.g. 'Albany Stamp Show'. Distinct from
    PhysicalLocation, which is where a stamp is stored (album/binder)."""
    __tablename__ = "origin_locations"

    id   = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, unique=True, nullable=False)

    origins = relationship("StampCopyOrigin", back_populates="location")


class Dealer(Base):
    """A seller/source a copy was acquired from."""
    __tablename__ = "dealers"

    id   = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, unique=True, nullable=False)

    origins = relationship("StampCopyOrigin", back_populates="dealer")


class StampCopyOrigin(Base):
    """Provenance for a single StampCopy: where and how it was acquired."""
    __tablename__ = "stamp_copy_origins"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    copy_id       = Column(Integer, ForeignKey("stamp_copies.id", ondelete="CASCADE"),
                           nullable=False, unique=True)
    location_id   = Column(Integer, ForeignKey("origin_locations.id", ondelete="SET NULL"),
                           nullable=True)
    dealer_id     = Column(Integer, ForeignKey("dealers.id", ondelete="SET NULL"),
                           nullable=True)
    method        = Column(String, nullable=True)   # bought / given / traded / ...
    price         = Column(Numeric(10, 2), nullable=True)
    acquired_date = Column(DateTime, nullable=True)
    notes         = Column(String, nullable=True)

    copy     = relationship("StampCopy", back_populates="origin")
    location = relationship("OriginLocation", back_populates="origins")
    dealer   = relationship("Dealer", back_populates="origins")


class StampImage(Base):
    __tablename__ = 'stamp_images'

    id = Column(Integer, primary_key=True)
    stamp_id = Column(Integer, ForeignKey('stamps.id', ondelete="CASCADE"), nullable=False)
    file_path = Column(String, nullable=False)  # Path to local image

    stamp = relationship("Stamp", back_populates="images")


class Theme(Base):
    __tablename__ = "themes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, unique=True, nullable=False)

    # Relationship to stamps
    stamps = relationship(
        "Stamp",
        secondary=stamp_theme_association,
        back_populates="themes"
    )