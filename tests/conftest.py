"""
tests/conftest.py

Shared fixtures for the test suite.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture(autouse=True)
def _test_db(tmp_path, monkeypatch):
    """
    Redirect all DB operations to an isolated temp SQLite file for each test.

    Patches db.models.{engine, SessionLocal} and db.crud.SessionLocal so every
    crud.get_db() call goes to the temp DB instead of the real agent.db.
    """
    from db import crud, models
    from db.models import Base

    db_url = f"sqlite:///{tmp_path / 'test.db'}"
    new_engine = create_engine(db_url, connect_args={"check_same_thread": False})
    NewSL = sessionmaker(bind=new_engine, autocommit=False, autoflush=False, expire_on_commit=False)

    Base.metadata.create_all(bind=new_engine)

    monkeypatch.setattr(models, "engine", new_engine)
    monkeypatch.setattr(models, "SessionLocal", NewSL)
    monkeypatch.setattr(crud, "SessionLocal", NewSL)

    yield
