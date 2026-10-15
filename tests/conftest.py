"""Test database fixture.

Default: an in-memory mongomock database (no server needed). It covers CRUD, indexes and unique-key behaviour,
but NOT aggregation features such as $setWindowFields / $densify, nor explain plans.
Set TEST_MONGO_URI (for example mongodb://localhost:27017) to run the same tests on a real MongoDB; each test
gets its own throw-away database, dropped afterwards. Tests marked `real_mongo` only run in that mode.
"""
import os
import uuid

import pytest

REAL = bool(os.getenv("TEST_MONGO_URI"))


def pytest_configure(config):
    config.addinivalue_line("markers", "real_mongo: needs a real MongoDB (set TEST_MONGO_URI)")


def pytest_collection_modifyitems(config, items):
    if REAL:
        return
    skip = pytest.mark.skip(reason="needs a real MongoDB: set TEST_MONGO_URI")
    for item in items:
        if "real_mongo" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def db(monkeypatch):
    import app.main as m

    name = f"attendance_test_{uuid.uuid4().hex[:8]}"
    if REAL:
        from pymongo import MongoClient

        cl = MongoClient(os.environ["TEST_MONGO_URI"], tz_aware=True, serverSelectionTimeoutMS=5000)
        database = cl[name]
        monkeypatch.setattr(m, "db", database)
        yield database
        cl.drop_database(name)
        cl.close()
    else:
        import mongomock

        database = mongomock.MongoClient(tz_aware=True)[name]
        monkeypatch.setattr(m, "db", database)
        yield database


@pytest.fixture
def adb(monkeypatch):
    """Database for the analytics tests. With TEST_MONGO_URI: a real MongoDB (the authoritative run). Without it: mongomock
    storage with aggregate() executed by tests/mini_mongo.py, because mongomock cannot run the contract's pipelines."""
    import app.main as m

    name = f"attendance_test_{uuid.uuid4().hex[:8]}"
    if REAL:
        from pymongo import MongoClient

        cl = MongoClient(os.environ["TEST_MONGO_URI"], tz_aware=True, serverSelectionTimeoutMS=5000)
        database = cl[name]
        monkeypatch.setattr(m, "db", database)
        yield database
        cl.drop_database(name)
        cl.close()
    else:
        import mongomock

        from tests.mini_mongo import EmulatedDb

        database = EmulatedDb(mongomock.MongoClient(tz_aware=True)[name])
        monkeypatch.setattr(m, "db", database)
        yield database
