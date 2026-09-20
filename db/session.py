from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .base import Base
from . import models  #models are imported so their tables register with Base.metadata
from config import DATABASE_URL
from sqlalchemy import inspect
from logger import logger


engine = create_engine(
    DATABASE_URL,
    echo=False,  #set True to log every statement; noisy and slow on bulk reads like /api/sync/data
)

# Debug: log the effective database URL and existing tables at import time
try:
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    logger.info(f"Existing tables: {tables}")
except Exception as e:
    # Avoid raising here; just want diagnostic information
    try:
        print("DB debug error:", e)
    except Exception:
        pass

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    future=True,
)


def init_db():
    """Create all tables."""
    logger.info("Initializing database schema...")
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    logger.info(f"Database tables after init: {tables}")


# Auto-create missing tables when importing the session module.
# Makes sure app can start even if init_db() was not run manually.
try:
    init_db()
except Exception as e:
    logger.error(f"Failed to initialize database schema on import: {e}")
