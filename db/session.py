from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .base import Base


DATABASE_URL = (
   # "postgresql+psycopg2://stamp_admin:adminpass@localhost:5432/stamps"
    "postgresql+psycopg2://stamp_admin:adminpass@localhost:5432/stamps"

)

engine = create_engine(
    DATABASE_URL,
    echo=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    future=True,
)

def init_db():
    """Create all tables."""
    Base.metadata.create_all(bind=engine)
