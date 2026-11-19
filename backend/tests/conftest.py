"""Test setup shared by every test file.

Protections applied to every test automatically:
- Every module that talks to the database is pointed at a throwaway database, so a test can never
  read or change the developer's real data, even if it forgets to set up its own. (Tests that need
  specific data still point the modules at their own database, which simply replaces this one.)
- The background scheduler (automatic mode) is switched off, so starting the app in a test can't
  start real work on its own.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import automation, cli, editor, export, fullvideo, health, judge, pipeline, runtime, tuning, youtube
from app import db as dbmod
from app import main as mainmod
from app.config import settings

MODULES_USING_THE_DATABASE = (cli, pipeline, judge, mainmod, editor, fullvideo, automation, tuning, export, runtime, youtube)


@pytest.fixture(autouse=True)
def _isolated_database(tmp_path_factory, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp('isolated') / 'isolated.db'}",
                           connect_args={"check_same_thread": False})
    dbmod.init_db(engine)
    session = sessionmaker(engine, expire_on_commit=False)
    for mod in MODULES_USING_THE_DATABASE:
        monkeypatch.setattr(mod, "SessionLocal", session)
    for mod in (mainmod, cli):  # start-up also creates/upgrades tables: that must hit the throwaway engine too
        monkeypatch.setattr(mod, "init_db", lambda *a, **k: dbmod.init_db(engine))
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    health.invalidate_storage_cache()  # folder sizes are cached for a few seconds: never carry them between tests
