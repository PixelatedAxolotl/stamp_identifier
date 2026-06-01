from sqlalchemy import (
    Boolean,
    Column,
    Integer,
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