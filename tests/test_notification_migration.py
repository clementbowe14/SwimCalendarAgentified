from uuid import UUID

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app import db
from tests.db_test_utils import TEST_DATABASE_URL


def test_upgrade_backfills_all_appointments_and_raw_insert_default(migrated_postgres_db,monkeypatch):
    config=Config("alembic.ini")
    config.set_main_option("sqlalchemy.url",TEST_DATABASE_URL)
    monkeypatch.setenv("SWIM_DATABASE_URL",TEST_DATABASE_URL)
    command.downgrade(config,"20260906_0006")
    with db.engine.begin() as connection:
        for event_type in ("lesson", "hold", "block"):
            connection.execute(text("""
                INSERT INTO appointments (id,instructor_id,client_id,title,start_time,end_time,
                    location_address,lat,lng,is_break,event_type,status,created_at,updated_at)
                SELECT :id,instructor_id,client_id,'Legacy',start_time,end_time,
                    location_address,lat,lng,:is_break,:event_type,'cancelled',now(),now()
                FROM appointments LIMIT 1
            """), {"id": "legacy-" + event_type, "is_break": event_type == "block", "event_type": event_type})
        before=connection.execute(text("SELECT id FROM appointments")).scalars().all()
        assert before
    command.upgrade(config,"head")
    with db.engine.begin() as connection:
        rows=connection.execute(text("SELECT id,calendar_uid FROM appointments")).all()
        assert {r.id for r in rows}==set(before)
        assert {"legacy-lesson", "legacy-hold", "legacy-block"}.issubset({r.id for r in rows})
        assert len({r.calendar_uid for r in rows})==len(rows)
        for row in rows:
            assert str(UUID(row.calendar_uid))==row.calendar_uid
        assert connection.execute(text("SELECT count(*) FROM notification_events")).scalar()==0
        uid=connection.execute(text("""
            INSERT INTO appointments (id,instructor_id,client_id,title,start_time,end_time,
                location_address,lat,lng,is_break,event_type,status,created_at,updated_at)
            SELECT 'raw-uid-test',instructor_id,client_id,'Test',start_time,end_time,
                location_address,lat,lng,false,'lesson','cancelled',now(),now()
            FROM appointments LIMIT 1 RETURNING calendar_uid
        """)).scalar_one()
        assert str(UUID(uid))==uid
        assert uid not in {r.calendar_uid for r in rows}
