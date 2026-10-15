"""Tests for the test-only aggregation emulator (tests/mini_mongo.py): documented MongoDB behaviour, plus agreement with
mongomock on the operators both implement. If these fail, the analytics tests that rely on the emulator mean nothing."""
import datetime as dt

import mongomock
import pytest

from tests.mini_mongo import EmulatedDb, MISSING, compare, eval_expr, match_doc, run_pipeline

UTC = dt.timezone.utc


def run(docs, pipeline, collections=None):
    return run_pipeline(docs, pipeline, lambda name: (collections or {})[name])


# ------------------------------------------------------------------ documented behaviour
def test_rank_is_standard_competition_ranking_with_skipped_ranks():
    docs = [{"s": 10}, {"s": 30}, {"s": 20}, {"s": 20}]
    out = run(docs, [{"$setWindowFields": {"sortBy": {"s": -1}, "output": {"rank": {"$rank": {}}}}}])
    assert [(d["s"], d["rank"]) for d in out] == [(30, 1), (20, 2), (20, 2), (10, 4)]  # 1, 2, 2, 4
    out = run([{"s": 5}, {"s": 5}, {"s": 5}], [{"$setWindowFields": {"sortBy": {"s": -1}, "output": {"rank": {"$rank": {}}}}}])
    assert [d["rank"] for d in out] == [1, 1, 1]


def test_window_average_over_current_and_up_to_six_preceding_rows():
    docs = [{"i": i, "v": float(i)} for i in range(1, 9)]
    out = run(docs, [{"$setWindowFields": {"sortBy": {"i": 1}, "output": {"m": {"$avg": "$v", "window": {"documents": [-6, 0]}}}}}])
    assert out[0]["m"] == 1.0 and out[1]["m"] == 1.5  # shorter windows at the start
    assert out[6]["m"] == 4.0 and out[7]["m"] == 5.0  # rows 2..8 -> mean 5.0; the first row has dropped out of the window


def test_window_average_ignores_nulls_and_is_null_when_nothing_is_numeric():
    docs = [{"i": 1, "v": None}, {"i": 2, "v": None}, {"i": 3, "v": 0.5}, {"i": 4, "v": None}]
    out = run(docs, [{"$setWindowFields": {"sortBy": {"i": 1}, "output": {"m": {"$avg": "$v", "window": {"documents": [-6, 0]}}}}}])
    assert [d["m"] for d in out] == [None, None, 0.5, 0.5]


def test_day_of_week_numbering_sunday_is_1_saturday_is_7():
    days = {dt.datetime(2026, 7, 5, tzinfo=UTC): 1, dt.datetime(2026, 7, 6, tzinfo=UTC): 2, dt.datetime(2026, 7, 10, tzinfo=UTC): 6,
            dt.datetime(2026, 7, 11, tzinfo=UTC): 7}
    for day, expected in days.items():
        assert eval_expr({"$dayOfWeek": "$d"}, {"d": day}, {}) == expected


def test_adding_milliseconds_to_a_date_gives_a_date_and_dates_format_with_zero_padding():
    start = dt.datetime(2026, 7, 6, tzinfo=UTC)
    nxt = eval_expr({"$add": ["$d", {"$multiply": ["$i", 86_400_000]}]}, {"d": start, "i": 3}, {})
    assert nxt == dt.datetime(2026, 7, 9, tzinfo=UTC)
    assert eval_expr({"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}}, {"d": nxt}, {}) == "2026-07-09"
    assert eval_expr({"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}}, {"d": dt.datetime(1, 2, 3, tzinfo=UTC)}, {}) == "0001-02-03"


def test_range_map_and_array_element_at():
    assert eval_expr({"$range": [0, 3]}, {}, {}) == [0, 1, 2]
    assert eval_expr({"$map": {"input": {"$range": [0, 3]}, "as": "i", "in": {"$add": ["$$i", 10]}}}, {}, {}) == [10, 11, 12]
    assert eval_expr({"$arrayElemAt": ["$a", 0]}, {"a": [7, 8]}, {}) == 7
    assert eval_expr({"$arrayElemAt": ["$a", 0]}, {"a": []}, {}) is MISSING  # empty array: no element
    assert eval_expr({"$ifNull": [{"$arrayElemAt": ["$a", 0]}, 0]}, {"a": []}, {}) == 0


def test_null_and_missing_behave_like_the_server():
    assert eval_expr({"$ifNull": ["$x", 5]}, {}, {}) == 5 and eval_expr({"$ifNull": ["$x", 5]}, {"x": None}, {}) == 5
    assert eval_expr({"$ifNull": ["$x", 5]}, {"x": 0}, {}) == 0
    assert eval_expr({"$gt": ["$late", 0]}, {}, {}) is False  # missing is not > 0
    assert eval_expr({"$eq": ["$half_day", True]}, {}, {}) is False
    assert eval_expr({"$cond": [{"$eq": ["$h", True]}, 0.5, 1]}, {"h": True}, {}) == 0.5
    assert eval_expr({"$in": ["$s", ["A", "B"]]}, {"s": "B"}, {}) is True and eval_expr({"$in": ["$s", ["A"]]}, {}, {}) is False


def test_match_uses_type_bracketing_for_ranges():
    assert match_doc({"late": 5}, {"late": {"$gt": 0}}, {}) is True
    assert match_doc({}, {"late": {"$gt": 0}}, {}) is False and match_doc({"late": None}, {"late": {"$gt": 0}}, {}) is False
    assert match_doc({"late": "5"}, {"late": {"$gt": 0}}, {}) is False  # a string is not in the number bracket
    assert match_doc({"d": "2026-07-06"}, {"d": {"$gte": "2026-07-01", "$lt": "2026-08-01"}}, {}) is True
    assert match_doc({"d": "2026-08-01"}, {"d": {"$gte": "2026-07-01", "$lt": "2026-08-01"}}, {}) is False
    assert match_doc({"c": "B"}, {"c": {"$in": ["A", "B"]}}, {}) is True
    assert match_doc({"e": {"department": "X"}}, {"e.department": "X"}, {}) is True


def test_group_sum_ignores_non_numbers_and_count_of_empty_is_no_document():
    docs = [{"k": "a", "v": 2}, {"k": "a", "v": None}, {"k": "a"}, {"k": "b", "v": 1.5}]
    out = {d["_id"]: d["t"] for d in run(docs, [{"$group": {"_id": "$k", "t": {"$sum": "$v"}}}])}
    assert out == {"a": 2, "b": 1.5}
    assert run([{"a": 1}], [{"$group": {"_id": None, "n": {"$sum": 1}}}]) == [{"_id": None, "n": 1}]
    assert run([], [{"$group": {"_id": None, "n": {"$sum": 1}}}]) == []  # an empty input yields no group at all
    assert run([], [{"$count": "n"}]) == []


def test_unwind_edge_cases():
    docs = [{"a": [1, 2]}, {"a": []}, {"a": None}, {}, {"a": 3}]
    assert [d["a"] for d in run(docs, [{"$unwind": "$a"}])] == [1, 2, 3]  # a scalar counts as a one-element array
    assert len(run(docs, [{"$unwind": {"path": "$a", "preserveNullAndEmptyArrays": True}}])) == 6


def test_lookup_forms():
    people = [{"code": "A", "dept": "X", "joined": "2026-01-01"}, {"code": "B", "dept": "X", "joined": "2026-07-07"}, {"code": "C", "dept": "Y", "joined": "2026-01-01"}]
    logs = [{"code": "A", "d": "2026-07-06", "n": 1}, {"code": "A", "d": "2026-07-07", "n": 2}, {"code": "Z", "d": "2026-07-06", "n": 9}]
    cols = {"people": people, "logs": logs}
    out = run(people, [{"$lookup": {"from": "logs", "localField": "code", "foreignField": "code", "as": "l"}}], cols)
    assert [len(d["l"]) for d in out] == [2, 0, 0]
    out = run(people, [{"$lookup": {"from": "logs", "localField": "code", "foreignField": "code",
                                    "pipeline": [{"$match": {"d": {"$gte": "2026-07-07"}}}, {"$group": {"_id": None, "t": {"$sum": "$n"}}}], "as": "m"}}], cols)
    assert [d["m"][0]["t"] if d["m"] else None for d in out] == [2, None, None]
    days = [{"date": "2026-07-06"}, {"date": "2026-07-07"}]
    out = run(days, [{"$lookup": {"from": "people", "let": {"d": "$date"}, "pipeline": [
        {"$match": {"dept": "X", "$expr": {"$lte": ["$joined", "$$d"]}}}, {"$count": "n"}], "as": "h"}}], cols)
    assert [d["h"][0]["n"] for d in out] == [1, 2]  # B has not joined on the 6th; has on the 7th
    out = run([{"codes": ["A", "C"]}], [{"$lookup": {"from": "people", "localField": "codes", "foreignField": "code", "as": "p"}}], cols)
    assert len(out[0]["p"]) == 2  # an array local field matches any element


def test_project_and_add_fields_semantics():
    docs = [{"_id": 1, "a": 1, "b": {"c": 2}, "z": 9}]
    assert run(docs, [{"$project": {"_id": 0, "a": 1, "x": "$b.c"}}]) == [{"a": 1, "x": 2}]
    assert run(docs, [{"$project": {"z": 0}}]) == [{"_id": 1, "a": 1, "b": {"c": 2}}]
    assert run(docs, [{"$addFields": {"s": {"$add": ["$a", "$b.c"]}}}])[0]["s"] == 3


def test_sort_orders_null_before_numbers_and_breaks_ties_with_later_keys():
    docs = [{"n": 2, "k": "b"}, {"n": None, "k": "z"}, {"n": 2, "k": "a"}, {"n": 5, "k": "c"}]
    assert [d["k"] for d in run(docs, [{"$sort": {"n": -1, "k": 1}}])] == ["c", "a", "b", "z"]


def test_compare_follows_bson_type_order():
    assert compare(None, 0) < 0 < compare("a", 5) and compare(5, 5.0) == 0 and compare(True, "x") > 0 and compare(MISSING, None) == 0


# ------------------------------------------------------------------ the emulator refuses what it does not model
@pytest.mark.parametrize("pipeline", [[{"$densify": {"field": "x"}}], [{"$unionWith": "x"}], [{"$facet": {}}],
                                      [{"$project": {"a": {"$round": [1.5, 0]}}}],
                                      [{"$setWindowFields": {"partitionBy": "$a", "sortBy": {"a": 1}, "output": {}}}]])
def test_unmodelled_features_raise_instead_of_passing_silently(pipeline):
    with pytest.raises(NotImplementedError):
        run([{"a": 1}], pipeline)


# ------------------------------------------------------------------ agreement with mongomock on the shared subset
SHARED = [
    [{"$match": {"d": {"$gte": "2026-07-02", "$lt": "2026-07-08"}}}, {"$group": {"_id": "$e", "n": {"$sum": 1}, "late": {"$sum": {"$cond": [{"$gt": ["$late", 0]}, 1, 0]}}}}],
    [{"$group": {"_id": None, "t": {"$sum": {"$ifNull": ["$late", 0]}}}}],
    [{"$match": {"late": {"$gt": 0}}}, {"$group": {"_id": "$e", "t": {"$sum": "$late"}}}, {"$lookup": {"from": "emp", "localField": "_id", "foreignField": "e", "as": "x"}}, {"$unwind": "$x"}],
    [{"$match": {"st": {"$in": ["PRESENT", "WFH"]}}}, {"$sort": {"d": -1, "e": 1}}, {"$project": {"_id": 0, "e": 1, "d": 1}}, {"$limit": 4}],
    [{"$group": {"_id": "$d", "p": {"$sum": {"$cond": [{"$and": [{"$in": ["$st", ["PRESENT", "WFH"]]}, {"$eq": ["$hd", True]}]}, 0.5, 1]}}}}],
]


@pytest.fixture
def twin_data():
    logs = [{"e": e, "d": f"2026-07-{d:02d}", "late": late, "st": st, "hd": hd}
            for e, d, late, st, hd in [("A", 1, 0, "PRESENT", False), ("A", 2, 12, "PRESENT", True), ("A", 3, 0, "LEAVE", False),
                                       ("B", 2, 30, "WFH", False), ("B", 3, 0, "ABSENT", False), ("B", 6, 5, "PRESENT", False),
                                       ("C", 6, 0, "WFH", True), ("Z", 7, 8, "PRESENT", False)]]
    logs.append({"e": "C", "d": "2026-07-07", "st": "PRESENT"})  # legacy: no late / hd
    emp = [{"e": "A", "n": "Ann"}, {"e": "B", "n": "Bob"}, {"e": "C", "n": "Cy"}]
    mongo = mongomock.MongoClient()["t"]
    mongo.logs.insert_many(logs)
    mongo.emp.insert_many(emp)
    return mongo


@pytest.mark.parametrize("pipeline", SHARED)
def test_emulator_agrees_with_mongomock_where_both_implement_the_stages(twin_data, pipeline):
    def canonical(rows):
        out = []
        for r in rows:
            r = dict(r)
            if isinstance(r.get("x"), dict):  # the looked-up employee: compare everything but its ObjectId
                r["x"] = {k: v for k, v in r["x"].items() if k != "_id"}
            out.append(repr(sorted(r.items(), key=lambda kv: kv[0])))
        return sorted(out)

    expected = list(twin_data.logs.aggregate(pipeline))
    got = list(EmulatedDb(twin_data).logs.aggregate(pipeline))
    assert canonical(got) == canonical(expected)


def test_emulated_db_delegates_everything_but_aggregate(twin_data):
    edb = EmulatedDb(twin_data)
    assert edb.name == "t" and edb.logs.count_documents({}) == 9
    edb.emp.insert_one({"e": "D", "n": "Dee"})
    assert twin_data.emp.count_documents({}) == 4
    assert list(edb.emp.aggregate([{"$count": "n"}])) == [{"n": 4}]
