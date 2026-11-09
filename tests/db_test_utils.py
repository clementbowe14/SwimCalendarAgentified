import os

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app import db


TEST_DATABASE_URL = os.getenv(
    "SWIM_TEST_DATABASE_URL",
    "postgresql+psycopg://swim:swim@127.0.0.1:5432/swim_scheduler_test",
)


def reset_postgres_database(seed: bool = True) -> None:
    engine = create_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    try:
        with engine.begin() as connection:
            if connection.dialect.name != "postgresql":
                raise RuntimeError("Tests require a PostgreSQL database.")
            connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
            connection.execute(text("GRANT ALL ON SCHEMA public TO swim"))
            connection.execute(text("GRANT ALL ON SCHEMA public TO public"))
    except OperationalError as exc:
        if os.getenv("SWIM_REQUIRE_POSTGRES_TESTS", "").strip().lower() in {"1", "true", "yes", "on"}:
            raise
        pytest.skip(f"PostgreSQL test database is not available: {exc}")
    finally:
        engine.dispose()

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    previous_database_url = os.environ.get("SWIM_DATABASE_URL")
    os.environ["SWIM_DATABASE_URL"] = TEST_DATABASE_URL
    try:
        command.upgrade(config, "head")
    finally:
        if previous_database_url is None:
            os.environ.pop("SWIM_DATABASE_URL", None)
        else:
            os.environ["SWIM_DATABASE_URL"] = previous_database_url
    db.configure_database(TEST_DATABASE_URL)
    if seed:
        db.init_db()
