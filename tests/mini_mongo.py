"""A small emulator of the MongoDB aggregation subset this project's analytics pipelines use.

WHY IT EXISTS: mongomock cannot run $lookup with let/pipeline, $setWindowFields, $dateToString, $dayOfWeek, $range, $map ...
and no real MongoDB server is available in every environment. This module lets the pipeline LOGIC (stage order, field names,
grouping, arithmetic) be exercised without a server.

WHAT IT IS NOT: proof that a real server accepts the syntax. The same analytics tests run unchanged against a real MongoDB
when TEST_MONGO_URI is set (see conftest.py); that run is the authority.

Safety: any stage, operator or option it does not model raises NotImplementedError, so an unmodelled feature can never
silently "pass". It is test-only and is never imported by app/main.py.
"""
import copy
import datetime as dt
from functools import cmp_to_key

UTC = dt.timezone.utc


class _Missing:
    def __repr__(self):
        return "MISSING"

    def __bool__(self):
        return False


MISSING = _Missing()


# ----------------------------------------------------------------------------- BSON-style comparison
def _norm(v):
    if v is MISSING:
        return None
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)
    return v


def _rank(v):
    """BSON comparison order for the types in play: null < numbers < strings < objects < arrays < booleans < dates."""
    if v is None:
        return 1
    if isinstance(v, bool):
        return 8
    if isinstance(v, (int, float)):
        return 2
    if isinstance(v, str):
        return 3
    if isinstance(v, dict):
        return 4
    if isinstance(v, list):
        return 5
    if isinstance(v, dt.datetime):
        return 9
    return 7


def compare(a, b):
    a, b = _norm(a), _norm(b)
    ra, rb = _rank(a), _rank(b)
    if ra != rb:
        return -1 if ra < rb else 1
    if ra == 1:
        return 0
    if ra == 5:
        for x, y in zip(a, b):
            c = compare(x, y)
            if c:
                return c
        return (len(a) > len(b)) - (len(a) < len(b))
    if ra == 4:
        return (repr(sorted(a.items())) > repr(sorted(b.items()))) - (repr(sorted(a.items())) < repr(sorted(b.items())))
    return (a > b) - (a < b)


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# ----------------------------------------------------------------------------- paths
def get_path(value, path):
    return _walk(value, path.split("."))


def _walk(cur, parts):
    if not parts:
        return cur
    if isinstance(cur, list):
        out = []
        for element in cur:
            r = _walk(element, parts)
            if r is not MISSING:
                out.append(r)
        return out
    if isinstance(cur, dict):
        if parts[0] not in cur:
            return MISSING
        return _walk(cur[parts[0]], parts[1:])
    return MISSING


def set_path(doc, path, value):
    parts = path.split(".")
    for p in parts[:-1]:
        doc = doc.setdefault(p, {})
    doc[parts[-1]] = value


# ----------------------------------------------------------------------------- expressions
def truthy(v):
    if v is None or v is MISSING:
        return False
    if isinstance(v, bool):
        return v
    if _is_number(v):
        return v != 0
    return True


def eval_expr(expr, doc, variables):
    if isinstance(expr, str):
        if expr.startswith("$$"):
            name, _, rest = expr[2:].partition(".")
            if name not in variables:
                raise KeyError(f"undefined variable ${name}")
            return get_path(variables[name], rest) if rest else variables[name]
        if expr.startswith("$"):
            return get_path(doc, expr[1:])
        return expr
    if isinstance(expr, list):
        return [eval_expr(x, doc, variables) for x in expr]
    if isinstance(expr, dict):
        if len(expr) == 1 and next(iter(expr)).startswith("$"):
            return _apply_operator(next(iter(expr)), expr[next(iter(expr))], doc, variables)
        out = {}
        for k, v in expr.items():
            r = eval_expr(v, doc, variables)
            if r is not MISSING:
                out[k] = r
        return out
    return expr


def _args(raw, doc, variables):
    raw = raw if isinstance(raw, list) else [raw]
    return [eval_expr(a, doc, variables) for a in raw]


def _apply_operator(op, raw, doc, variables):
    ev = lambda e: eval_expr(e, doc, variables)
    if op == "$literal":
        return raw
    if op == "$cond":
        c, t, f = (raw["if"], raw["then"], raw["else"]) if isinstance(raw, dict) else raw
        return ev(t) if truthy(ev(c)) else ev(f)
    if op == "$and":
        return all(truthy(ev(a)) for a in raw)
    if op == "$or":
        return any(truthy(ev(a)) for a in raw)
    if op == "$not":
        return not truthy(ev(raw[0] if isinstance(raw, list) else raw))
    if op in ("$eq", "$ne", "$gt", "$gte", "$lt", "$lte"):
        a, b = _args(raw, doc, variables)
        c = compare(a, b)
        return {"$eq": c == 0, "$ne": c != 0, "$gt": c > 0, "$gte": c >= 0, "$lt": c < 0, "$lte": c <= 0}[op]
    if op == "$in":
        x, arr = _args(raw, doc, variables)
        if not isinstance(arr, list):
            raise ValueError("$in requires an array as a second argument")
        return any(compare(x, y) == 0 for y in arr)
    if op == "$ifNull":
        value, replacement = ev(raw[0]), raw[1]
        return ev(replacement) if value is None or value is MISSING else value
    if op == "$add":
        vals = _args(raw, doc, variables)
        if any(v is None or v is MISSING for v in vals):
            return None
        dates = [v for v in vals if isinstance(v, dt.datetime)]
        if dates:
            total = _norm(dates[0])
            for v in vals:
                if not isinstance(v, dt.datetime):
                    total += dt.timedelta(milliseconds=v)
            return total
        return sum(vals)
    if op == "$multiply":
        vals = _args(raw, doc, variables)
        if any(v is None or v is MISSING for v in vals):
            return None
        out = 1
        for v in vals:
            out *= v
        return out
    if op == "$divide":
        a, b = _args(raw, doc, variables)
        if a is None or a is MISSING or b is None or b is MISSING:
            return None
        return a / b  # ZeroDivisionError propagates, like the server's error
    if op == "$arrayElemAt":
        arr, idx = _args(raw, doc, variables)
        if arr is None or arr is MISSING:
            return None
        if not -len(arr) <= idx < len(arr):
            return MISSING
        return arr[idx]
    if op == "$range":
        a = _args(raw, doc, variables)
        return list(range(a[0], a[1], a[2] if len(a) > 2 else 1))
    if op == "$map":
        arr = ev(raw["input"])
        if arr is None or arr is MISSING:
            return None
        return [eval_expr(raw["in"], doc, {**variables, raw.get("as", "this"): el}) for el in arr]
    if op == "$size":
        arr = ev(raw[0] if isinstance(raw, list) else raw)
        if not isinstance(arr, list):
            raise ValueError("$size requires an array")
        return len(arr)
    if op == "$dateToString":
        if raw.get("format") != "%Y-%m-%d" or set(raw) - {"format", "date"}:
            raise NotImplementedError("only $dateToString {format: '%Y-%m-%d', date} is modelled")
        d = _norm(ev(raw["date"]))
        return None if d is None else f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
    if op == "$dayOfWeek":
        d = _norm(ev(raw))
        return None if d is None else (d.weekday() + 1) % 7 + 1  # Sunday = 1 ... Saturday = 7
    raise NotImplementedError(f"expression operator {op} is not modelled")


# ----------------------------------------------------------------------------- query ($match)
def _query_equal(value, target):
    if target is None:
        return value is None or value is MISSING
    if value is MISSING:
        return False
    if isinstance(value, list) and not isinstance(target, list):
        return any(_query_equal(v, target) for v in value)
    if isinstance(target, bool) != isinstance(value, bool):
        return False
    if _is_number(target) != _is_number(value) and not isinstance(target, bool):
        return False
    return compare(value, target) == 0


def _bracket(v):
    return 2 if _is_number(v) else (3 if isinstance(v, str) else (9 if isinstance(v, dt.datetime) else 0))


def _query_range(value, op, target):
    values = value if isinstance(value, list) else [value]
    for v in values:
        if v is MISSING or v is None or _bracket(v) != _bracket(target) or _bracket(v) == 0:
            continue
        c = compare(v, target)
        if {"$gt": c > 0, "$gte": c >= 0, "$lt": c < 0, "$lte": c <= 0}[op]:
            return True
    return False


def match_doc(doc, query, variables):
    for key, cond in query.items():
        if key == "$expr":
            if not truthy(eval_expr(cond, doc, variables)):
                return False
            continue
        if key == "$and":
            if not all(match_doc(doc, q, variables) for q in cond):
                return False
            continue
        if key == "$or":
            if not any(match_doc(doc, q, variables) for q in cond):
                return False
            continue
        value = get_path(doc, key)
        if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
            for op, target in cond.items():
                if op == "$eq":
                    ok = _query_equal(value, target)
                elif op == "$ne":
                    ok = not _query_equal(value, target)
                elif op in ("$gt", "$gte", "$lt", "$lte"):
                    ok = _query_range(value, op, target)
                elif op == "$in":
                    ok = any(_query_equal(value, t) for t in target)
                elif op == "$exists":
                    ok = (value is not MISSING) == bool(target)
                else:
                    raise NotImplementedError(f"query operator {op} is not modelled")
                if not ok:
                    return False
        elif not _query_equal(value, cond):
            return False
    return True


# ----------------------------------------------------------------------------- stages
def _sort_docs(docs, spec):
    def cmp(a, b):
        for field, direction in spec.items():
            c = compare(get_path(a, field), get_path(b, field))
            if c:
                return c * direction
        return 0

    return sorted(docs, key=cmp_to_key(cmp))


def _group_key(v):
    return repr(_norm(v))


def stage_group(docs, spec, get_coll, variables):
    groups, order = {}, []
    for d in docs:
        gid = eval_expr(spec["_id"], d, variables)
        gid = None if gid is MISSING else gid
        k = _group_key(gid)
        if k not in groups:
            groups[k] = {"_id": gid, "_docs": []}
            order.append(k)
        groups[k]["_docs"].append(d)
    out = []
    for k in order:
        g = groups[k]
        row = {"_id": g["_id"]}
        for field, acc in spec.items():
            if field == "_id":
                continue
            (op, expr), = acc.items()
            values = [eval_expr(expr, d, variables) for d in g["_docs"]]
            if op == "$sum":
                nums = [v for v in values if _is_number(v)]
                row[field] = sum(nums) if nums else 0
            elif op == "$push":
                row[field] = [v for v in values if v is not MISSING]
            elif op == "$first":
                row[field] = values[0] if values and values[0] is not MISSING else None
            else:
                raise NotImplementedError(f"accumulator {op} is not modelled")
        out.append(row)
    return out


def _lookup_values_match(foreign, local):
    f = None if foreign is MISSING else foreign
    l = None if local is MISSING else local
    if l is None:
        return f is None
    candidates = l if isinstance(l, list) else [l]
    fvals = f if isinstance(f, list) else [f]
    return any(_query_equal(fv, c) for c in candidates for fv in fvals)


def stage_lookup(docs, spec, get_coll, variables):
    allowed = {"from", "localField", "foreignField", "let", "pipeline", "as"}
    if set(spec) - allowed:
        raise NotImplementedError(f"$lookup options {set(spec) - allowed} are not modelled")
    out = []
    for d in docs:
        foreign = get_coll(spec["from"])
        local_vars = {**variables}
        if "let" in spec:
            local_vars.update({k: eval_expr(v, d, variables) for k, v in spec["let"].items()})
        if "localField" in spec:
            local = get_path(d, spec["localField"])
            foreign = [f for f in foreign if _lookup_values_match(get_path(f, spec["foreignField"]), local)]
        if "pipeline" in spec:
            foreign = run_pipeline(foreign, spec["pipeline"], get_coll, local_vars)
        row = dict(d)
        row[spec["as"]] = copy.deepcopy(foreign)
        out.append(row)
    return out


def stage_unwind(docs, spec, get_coll, variables):
    path, preserve = (spec, False) if isinstance(spec, str) else (spec["path"], spec.get("preserveNullAndEmptyArrays", False))
    if not isinstance(spec, str) and set(spec) - {"path", "preserveNullAndEmptyArrays"}:
        raise NotImplementedError("unmodelled $unwind option")
    field = path[1:]
    out = []
    for d in docs:
        value = get_path(d, field)
        if value is MISSING or value is None or value == []:
            if preserve:
                row = copy.deepcopy(d)
                out.append(row)
            continue
        elements = value if isinstance(value, list) else [value]
        for el in elements:
            row = copy.deepcopy(d)
            set_path(row, field, el)
            out.append(row)
    return out


def _projection_value(v):
    return v is True or v == 1 and not isinstance(v, (dict, str, list)) and v is not False


def stage_project(docs, spec, get_coll, variables):
    include_id = spec.get("_id", 1) not in (0, False)
    keys = {k: v for k, v in spec.items() if k != "_id"}
    exclusion = bool(keys) and all((not isinstance(v, (dict, str, list))) and v in (0, False) for v in keys.values())
    out = []
    for d in docs:
        if exclusion:
            row = copy.deepcopy(d)
            for k in keys:
                row.pop(k, None)
            if not include_id:
                row.pop("_id", None)
        else:
            row = {}
            if include_id and "_id" in d:
                row["_id"] = d["_id"]
            for k, v in keys.items():
                if not isinstance(v, (dict, str, list)) and v in (1, True):
                    val = get_path(d, k)
                else:
                    val = eval_expr(v, d, variables)
                if val is not MISSING:
                    set_path(row, k, copy.deepcopy(val))
        out.append(row)
    return out


def stage_add_fields(docs, spec, get_coll, variables):
    out = []
    for d in docs:
        row = copy.deepcopy(d)
        for k, v in spec.items():
            val = eval_expr(v, d, variables)
            if val is not MISSING:
                set_path(row, k, val)
        out.append(row)
    return out


def stage_set_window_fields(docs, spec, get_coll, variables):
    if set(spec) - {"sortBy", "output"}:
        raise NotImplementedError("$setWindowFields: partitionBy and other options are not modelled")
    sort_by = spec["sortBy"]
    ordered = _sort_docs(docs, sort_by)
    (sort_field, direction), = sort_by.items() if len(sort_by) == 1 else (None, None)
    out = [copy.deepcopy(d) for d in ordered]
    for field, definition in spec["output"].items():
        if "$rank" in definition:
            if sort_field is None:
                raise ValueError("$rank requires a single sortBy field")
            first_index = {}
            for i, d in enumerate(ordered):
                key = _group_key(get_path(d, sort_field))
                first_index.setdefault(key, i)
            for i, d in enumerate(ordered):
                out[i][field] = first_index[_group_key(get_path(d, sort_field))] + 1  # ties share a rank, the next is skipped
        elif "$avg" in definition:
            lo, hi = definition["window"]["documents"]
            if set(definition["window"]) != {"documents"}:
                raise NotImplementedError("only document windows are modelled")
            lo = 0 if lo == "current" else lo
            hi = 0 if hi == "current" else hi
            for i, d in enumerate(ordered):
                lo_i = 0 if lo == "unbounded" else max(0, i + lo)
                hi_i = len(ordered) - 1 if hi == "unbounded" else min(len(ordered) - 1, i + hi)
                vals = [eval_expr(definition["$avg"], ordered[j], variables) for j in range(lo_i, hi_i + 1)]
                nums = [v for v in vals if _is_number(v)]
                out[i][field] = (sum(nums) / len(nums)) if nums else None
        else:
            raise NotImplementedError(f"window operator in {definition} is not modelled")
    return out


STAGES = {
    "$match": lambda docs, spec, gc, v: [d for d in docs if match_doc(d, spec, v)],
    "$group": stage_group,
    "$lookup": stage_lookup,
    "$unwind": stage_unwind,
    "$project": stage_project,
    "$addFields": stage_add_fields,
    "$set": stage_add_fields,
    "$sort": lambda docs, spec, gc, v: _sort_docs(docs, spec),
    "$limit": lambda docs, spec, gc, v: docs[:spec],
    "$skip": lambda docs, spec, gc, v: docs[spec:],
    "$count": lambda docs, spec, gc, v: [{spec: len(docs)}] if docs else [],
    "$setWindowFields": stage_set_window_fields,
}


def run_pipeline(docs, pipeline, get_coll, variables=None):
    variables = variables or {}
    docs = copy.deepcopy(docs)
    for stage in pipeline:
        if len(stage) != 1:
            raise ValueError(f"a pipeline stage must have exactly one key: {stage}")
        name = next(iter(stage))
        if name not in STAGES:
            raise NotImplementedError(f"stage {name} is not modelled")
        docs = STAGES[name](docs, stage[name], get_coll, variables)
    return docs


# ----------------------------------------------------------------------------- database wrapper
_DB_ATTRIBUTES = {"name", "command", "client", "list_collection_names", "drop_collection", "create_collection", "get_collection", "codec_options"}


class EmulatedCollection:
    """A mongomock collection whose aggregate() runs through the emulator; everything else is delegated unchanged."""

    def __init__(self, real, database):
        self._real, self._database = real, database

    def aggregate(self, pipeline, **kwargs):
        docs = list(self._real.find({}))
        return iter(run_pipeline(docs, pipeline, self._database.documents_of))

    def __getattr__(self, name):
        return getattr(self._real, name)


class EmulatedDb:
    def __init__(self, real):
        self._real = real

    def documents_of(self, name):
        return list(self._real[name].find({}))

    def __getitem__(self, name):
        return EmulatedCollection(self._real[name], self)

    def __getattr__(self, name):
        if name in _DB_ATTRIBUTES:
            return getattr(self._real, name)
        return self[name]
