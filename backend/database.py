"""
Database Module: Handles SQLAlchemy engine configuration, session management,
and automatic schema/database initialization.
"""

import os
from sqlalchemy import create_engine, text, exc
from sqlalchemy.engine import url
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

load_dotenv()

import logging
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Create a backend/.env file (see .env.example) "
        "with a valid PostgreSQL connection string."
    )

# Managed Postgres providers (Neon/RDS/etc.) rarely grant permission to connect
# to the 'postgres' admin database or run CREATE DATABASE. Skip that step there
# (SKIP_DB_AUTOCREATE=true) and rely on the target database already existing.
SKIP_DB_AUTOCREATE = os.getenv("SKIP_DB_AUTOCREATE", "false").lower() == "true"

# pool_pre_ping: Neon (and any serverless/managed PG) drops idle connections;
# without this the first query after an idle spell throws. pool_recycle keeps
# connections younger than Neon's idle timeout. Small pool - the worker thread
# and request handlers share this engine against a free-tier instance.
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
    pool_size=5,
    max_overflow=5,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    """Dependency for obtaining a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def _ensure_database_exists():
    """Checks if the target database exists, and creates it if it doesn't."""
    target_url = url.make_url(DATABASE_URL)
    db_name = target_url.database

    # Construct a URL to the default 'postgres' database to perform administrative tasks
    admin_url = target_url.set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")

    with admin_engine.connect() as conn:
        try:
            # Check if database exists
            result = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :db_name"), {"db_name": db_name})
            if not result.scalar():
                logger.info(f"Creating database '{db_name}'...")
                conn.execute(text(f'CREATE DATABASE "{db_name}"'))
        except exc.OperationalError as e:
            logger.info(f"Database existence check skipped: {e}")

def init_db():
    """Initializes the database and tables."""
    if not SKIP_DB_AUTOCREATE:
        try:
            _ensure_database_exists()
        except Exception as e:
            logger.warning(f"Could not verify/create database: {e}")
    else:
        logger.info("SKIP_DB_AUTOCREATE set; assuming target database already exists.")

    import models  # Import here to avoid circular dependencies
    Base.metadata.create_all(bind=engine)