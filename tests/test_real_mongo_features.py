"""Preflight for a REAL MongoDB (skipped unless TEST_MONGO_URI is set). Each test runs one tiny pipeline using exactly one feature
the analytics endpoints depend on and checks the documented result, so an unsupported server fails here with a clear name instead
of failing hundreds of analytics tests. The emulator is NOT involved: these run on the server."""
import datetime as dt

import pytest

pytestmark = pytest.mark.real_mongo
UTC = dt.timezone.utc


@pytest.fixture
def coll(db):
    db.t.insert_many([{"n": 1, "s": 10, "d": "2026-07-06"}, {"n": 2, "s": 20, "d": "2026-07-07"}, {"n": 3, "s": 20, "d": "2026-07-06"}, {"n": 4, "s": 30, "d": "2026-07-08"}])
    db.people.insert_many([{"d": "2026-07-06", "dept": "X", "j": "2026-07-01"}, {"d": "x", "dept": "X", "j": "2026-07-07"}, {"d": "y", "dept": "Y", "j": "2026-01-01"}])
    return db.t


def test_server_is_6_or_newer(db):
    assert int(db.client.server_info()["version"].split(".")[0]) >= 6


def test_set_window_fields_rank_is_competition_ranking(coll):
    rows = list(coll.aggregate([{"$setWindowFields": {"sortBy": {"s": -1}, "output": {"rank": {"$rank": {}}}}}, {"$sort": {"n": 1}}]))
    assert [(r["n"], r["rank"]) for r in rows] == [(1, 4), (2, 2), (3, 2), (4, 1)]  # 1, 2, 2, 4


def test_set_window_fields_moving_average_with_a_document_window(coll):
    rows = list(coll.aggregate([{"$setWindowFields": {"sortBy": {"n": 1}, "output": {"m": {"$avg": "$s", "window": {"documents": [-2, 0]}}}}}, {"$sort": {"n": 1}}]))
    assert [r["m"] for r in rows] == [10, 15, pytest.approx(50 / 3), pytest.approx(70 / 3)]


def test_window_average_ignores_nulls_and_is_null_when_nothing_is_numeric(db):
    db.w.insert_many([{"i": 1, "v": None}, {"i": 2, "v": None}, {"i": 3, "v": 0.5}])
    rows = list(db.w.aggregate([{"$setWindowFields": {"sortBy": {"i": 1}, "output": {"m": {"$avg": "$v", "window": {"documents": [-6, 0]}}}}}, {"$sort": {"i": 1}}]))
    assert [r["m"] for r in rows] == [None, None, 0.5]


def test_lookup_with_local_field_foreign_field_and_a_pipeline(coll, db):
    rows = list(coll.aggregate([{"$lookup": {"from": "people", "localField": "d", "foreignField": "d", "as": "p",
                                             "pipeline": [{"$match": {"dept": "X"}}, {"$count": "n"}]}}, {"$sort": {"n": 1}}]))
    assert [r["p"] for r in rows] == [[{"n": 1}], [], [{"n": 1}], []]


def test_lookup_with_let_and_expr_inside_a_pipeline(coll):
    rows = list(coll.aggregate([{"$limit": 1}, {"$lookup": {"from": "people", "let": {"day": "2026-07-06"}, "as": "h",
                                                            "pipeline": [{"$match": {"dept": "X", "$expr": {"$lte": ["$j", "$$day"]}}}, {"$count": "n"}]}}]))
    assert rows[0]["h"] == [{"n": 1}]


def test_range_unwind_date_arithmetic_date_to_string_and_day_of_week(db):
    db.seed.insert_one({"k": 1})
    start = dt.datetime(2026, 7, 4, tzinfo=UTC)  # a Saturday
    rows = list(db.seed.aggregate([
        {"$project": {"_id": 0, "i": {"$range": [0, 3]}}}, {"$unwind": "$i"},
        {"$addFields": {"dt": {"$add": [start, {"$multiply": ["$i", 86_400_000]}]}}},
        {"$addFields": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dt"}}, "dow": {"$dayOfWeek": "$dt"}}},
        {"$sort": {"dt": 1}}, {"$project": {"_id": 0, "date": 1, "dow": 1}}]))
    assert rows == [{"date": "2026-07-04", "dow": 7}, {"date": "2026-07-05", "dow": 1}, {"date": "2026-07-06", "dow": 2}]  # Sat=7, Sun=1, Mon=2


def test_in_with_a_literal_array_and_array_element_at_on_an_array_of_subdocuments(coll):
    rows = list(coll.aggregate([{"$limit": 1}, {"$project": {"_id": 0, "w": {"$in": [2, [2, 3, 4, 5, 6]]}, "e": {"$ifNull": [{"$arrayElemAt": [[], 0]}, 0]},
                                                             "f": {"$arrayElemAt": [[{"n": 5}, {"n": 6}], 0]}}}]))
    assert rows == [{"w": True, "e": 0, "f": {"n": 5}}]
