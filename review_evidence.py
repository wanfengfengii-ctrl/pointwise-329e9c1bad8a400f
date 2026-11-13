"""Independent review evidence for the incremental materialization release.

Expectations in this module are computed by a self-contained bag-algebra
evaluator written for the review. It deliberately does NOT import rel, ivm,
store, samples or any of their helpers. The only use of the production stack is
harness.run, and solely to record the *observed* warehouse actuals.
"""
import json
from collections import Counter

import harness  # production driver; allowed only for recorded actuals


# --------------------------------------------------------------------------- #
# Independent bag algebra (full-refresh semantics, re-derived from CONTRACT.md)
# --------------------------------------------------------------------------- #
def pack(bag):
    return [[list(row), n] for row, n in sorted(bag.items(), key=lambda p: repr(p[0])) if n]


def diff(new, old):
    out = Counter()
    for row in new.keys() | old.keys():
        n = new[row] - old[row]
        if n:
            out[row] = n
    return out


def scalar(expr, row):
    tag = expr[0]
    if tag == "col":
        return row[expr[1]]
    if tag == "lit":
        return expr[1]
    if tag == "isnull":
        return scalar(expr[1], row) is None
    if tag == "not":
        v = scalar(expr[1], row)
        return None if v is None else not v
    x, y = scalar(expr[1], row), scalar(expr[2], row)
    if tag == "and":
        return False if x is False or y is False else (None if x is None or y is None else True)
    if tag == "or":
        return True if x is True or y is True else (None if x is None or y is None else False)
    if x is None or y is None:
        return None
    return x == y if tag == "eq" else x > y


def same(left_row, right_row, pairs):
    # NULL never equals anything, including NULL.
    return all(left_row[i] is not None and right_row[j] is not None
               and left_row[i] == right_row[j] for i, j in pairs)


def product(a, b, node, right_width, pad=False):
    out = Counter()
    for x, nx in a.items():
        matched = False
        for y, ny in b.items():
            if same(x, y, node["on"]):
                matched = True
                out[x + y] += nx * ny
        if pad and not matched:
            out[x + (None,) * right_width] += nx
    return out


def aggregate(rows, node):
    groups = {} if node["keys"] else {(): [0, 0, 0]}
    for row, n in rows.items():
        key = tuple(row[i] for i in node["keys"])
        state = groups.setdefault(key, [0, 0, 0])
        state[1] += n
        value = row[node["value"]]
        if value is not None:
            state[0] += n * value
            state[2] += n
    out = Counter()
    for key, (total, count, nonnull) in groups.items():
        value = total if nonnull else None
        if node["having"] is None or (value is not None and value > node["having"]):
            out[key + (value, count, nonnull)] = 1
    return out


def evaluate(plan, sources):
    states, widths = {}, {}
    for node in plan["nodes"]:
        ident, op = node["id"], node["op"]
        if op == "source":
            out = sources.get(ident, Counter()).copy()
            width = len(node["types"])
        elif op in ("inner", "left", "anti"):
            a, b = states[node["left"]], states[node["right"]]
            width_r = widths[node["right"]]
            if op == "anti":
                out = Counter({x: n for x, n in a.items()
                               if not any(same(x, y, node["on"]) for y in b)})
                width = widths[node["left"]]
            else:
                out = product(a, b, node, width_r, op == "left")
                width = widths[node["left"]] + width_r
        elif op == "union":
            out = states[node["inputs"][0]].copy()
            for child in node["inputs"][1:]:
                out.update(states[child])
            width = widths[node["inputs"][0]]
        else:
            a = states[node["input"]]
            width = widths[node["input"]]
            if op == "project":
                out = Counter()
                for row, n in a.items():
                    out[tuple(row[i] for i in node["cols"])] += n
                width = len(node["cols"])
            elif op == "filter":
                out = Counter({r: n for r, n in a.items() if scalar(node["where"], r) is True})
            elif op == "distinct":
                out = Counter({r: 1 for r in a})
            elif op == "group":
                out = aggregate(a, node)
                width = len(node["keys"]) + 3
            else:
                raise ValueError("OP")
        states[ident], widths[ident] = out, width
    return states, widths


# --------------------------------------------------------------------------- #
# Independent revision coalescing: replay record identities to final source bags
# --------------------------------------------------------------------------- #
def source_histories(plan, batches):
    source_ids = [n["id"] for n in plan["nodes"] if n["op"] == "source"]
    current = {}
    history = []
    for batch in batches:
        for src, rid, rev, row in batch["changes"]:
            key = (src, rid)
            prior = current.get(key)
            if prior is not None and rev <= prior[0]:
                continue  # lower revisions are ignored; case data has no conflicts
            current[key] = (rev, row)
        bags = {s: Counter() for s in source_ids}
        for (src, _rid), (_rev, row) in current.items():
            if row is not None:
                bags[src][tuple(row)] += 1
        history.append(bags)
    return history


# --------------------------------------------------------------------------- #
# Case definitions (public plan/batch formats)
# --------------------------------------------------------------------------- #
SAMPLES_PLAN = {
    "nodes": [
        {"id": "readings", "op": "source", "types": ["s", "s", "i"]},
        {"id": "sites", "op": "source", "types": ["s", "s"]},
        {"id": "enriched", "op": "left", "left": "readings", "right": "sites", "on": [[0, 0]]},
        {"id": "daily", "op": "group", "input": "enriched", "keys": [1, 4], "value": 2,
         "having": None},
    ],
    "root": "daily",
}
NORMAL = [
    {"id": "dimensions", "changes": [["sites", "s1", 1, ["A", "east"]]]},
    {"id": "first", "changes": [["readings", "r1", 1, ["A", "d1", 10]]]},
    {"id": "second", "changes": [["readings", "r2", 1, ["A", "d1", 7]]]},
    {"id": "correct", "changes": [["readings", "r1", 2, ["A", "d1", 12]]]},
]
INCIDENT = [
    {"id": "arrival", "changes": [["readings", "r1", 1, ["A", "d1", 10]]]},
    {"id": "late-dimension", "changes": [["sites", "s1", 1, ["A", "east"]]]},
]


def _cases():
    return [
        # --- controls ------------------------------------------------------- #
        {
            "id": "C1-normal-recorded-business",
            "plan": SAMPLES_PLAN,
            "batches": NORMAL,
            "reopen_after": [0, 1, 2, 3],
            "locations": ["store.py:114", "ivm.py:33", "rel.py:78"],
            "note": "Recorded NORMAL business history with reconstruction after every "
                    "batch: left join, grouping and revision replacement all agree when "
                    "no support boundary is crossed.",
        },
        {
            "id": "C2-shared-subplan-union",
            "plan": {
                "nodes": [
                    {"id": "s", "op": "source", "types": ["i"]},
                    {"id": "p", "op": "project", "input": "s", "cols": [0]},
                    {"id": "u", "op": "union", "inputs": ["p", "p"]},
                ],
                "root": "u",
            },
            "batches": [
                {"id": "b1", "changes": [["s", "r1", 1, [5]]]},
                {"id": "b2", "changes": [["s", "r2", 1, [5]]]},
                {"id": "b3", "changes": [["s", "r3", 1, [7]]]},
            ],
            "reopen_after": [1],
            "locations": ["ivm.py:53", "ivm.py:72", "store.py:115"],
            "note": "One child feeds two union edges: shared subplan propagation keeps "
                    "multiplicities exact through duplicate source rows.",
        },
        {
            "id": "C3-null-join-keys",
            "plan": {
                "nodes": [
                    {"id": "s", "op": "source", "types": ["i"]},
                    {"id": "t", "op": "source", "types": ["i"]},
                    {"id": "a", "op": "anti", "left": "s", "right": "t", "on": [[0, 0]]},
                    {"id": "lj", "op": "left", "left": "s", "right": "t", "on": [[0, 0]]},
                ],
                "root": "lj",
            },
            "batches": [
                {"id": "b1", "changes": [["s", "r1", 1, [None]], ["t", "r1", 1, [None]]]},
                {"id": "b2", "changes": [["s", "r2", 1, [1]], ["s", "r3", 1, [None]]]},
                {"id": "b3", "changes": [["t", "r2", 1, [2]]]},
                {"id": "b4", "changes": [["t", "r3", 1, [None]]]},
                {"id": "b5", "changes": [["s", "r4", 1, [2]]]},
            ],
            "reopen_after": [3],
            "locations": ["rel.py:46", "rel.py:47", "ivm.py:46"],
            "note": "NULL never equals NULL: null-key left rows stay padded and anti-kept, "
                    "duplicate nulls never match, and a non-null key only flips when an "
                    "equality actually holds. No padding boundary is crossed by a right "
                    "change, so the outer-join retraction defect cannot mask the NULL "
                    "semantics.",
        },
        {
            "id": "B3-null-key-zero-sum-group",
            "plan": {
                "nodes": [
                    {"id": "s", "op": "source", "types": ["i", "i"]},
                    {"id": "g", "op": "group", "input": "s", "keys": [0], "value": 1,
                     "having": None},
                ],
                "root": "g",
            },
            "batches": [
                {"id": "b1", "changes": [
                    ["s", "r1", 1, [None, 5]],
                    ["s", "r2", 1, [None, -5]],
                    ["s", "r3", 1, [1, None]],
                ]},
                {"id": "b2", "changes": [["s", "r4", 1, [1, None]]]},
                {"id": "b3", "changes": [["s", "r1", 2, None]]},
                {"id": "b4", "changes": [["s", "r2", 2, None]]},
            ],
            "reopen_after": [0, 3],
            "locations": ["ivm.py:14", "ivm.py:21", "rel.py:61"],
            "note": "NULL grouping keys coalesce; a zero SUM with nonzero COUNT is a real "
                    "group; all-NULL value rows feed COUNT(*) but leave SUM NULL.",
        },
        {
            "id": "C5-tombstone-revive-receipt",
            "plan": {
                "nodes": [
                    {"id": "s", "op": "source", "types": ["i"]},
                    {"id": "f", "op": "filter", "input": "s",
                     "where": ["gt", ["col", 0], ["lit", 0]]},
                ],
                "root": "f",
            },
            "batches": [
                {"id": "insert", "changes": [["s", "r1", 1, [5]]]},
                {"id": "delete", "changes": [["s", "r1", 2, None]]},
                {"id": "revive", "changes": [["s", "r1", 3, [5]]]},
                {"id": "noop", "changes": []},
            ],
            "reopen_after": [2],
            "locations": ["store.py:84", "store.py:88", "store.py:119"],
            "note": "Tombstone then later revision revives a record through the filter; "
                    "an empty batch still advances generation with an empty delta. "
                    "Receipt replay idempotency for a repeated batch id is asserted "
                    "separately in checks.py (a replayed receipt carries the original "
                    "generation and therefore cannot equal a zero net delta).",
        },
        # --- mismatches ----------------------------------------------------- #
        {
            "id": "M1-incident-late-dimension",
            "plan": SAMPLES_PLAN,
            "batches": INCIDENT,
            "reopen_after": [0, 1],
            "locations": ["ivm.py:50", "ivm.py:51", "rel.py:57"],
            "note": "Recorded incident: right-side first match must retract the padded "
                    "left row; the engine adds the matched group but never retracts the "
                    "NULL extension.",
        },
        {
            "id": "M2-anti-right-only-change",
            "plan": {
                "nodes": [
                    {"id": "s", "op": "source", "types": ["i"]},
                    {"id": "t", "op": "source", "types": ["i"]},
                    {"id": "a", "op": "anti", "left": "s", "right": "t", "on": [[0, 0]]},
                ],
                "root": "a",
            },
            "batches": [
                {"id": "b1", "changes": [["s", "r1", 1, [1]]]},
                {"id": "b2", "changes": [["t", "r1", 1, [1]]]},
                {"id": "b3", "changes": [["t", "r1", 2, None]]},
            ],
            "reopen_after": [0, 2],
            "locations": ["ivm.py:43", "ivm.py:47", "ivm.py:48"],
            "note": "Anti join ignores right deltas entirely: right-only insert/delete "
                    "flips existence but the anti bag never changes, including after "
                    "SQLite reconstruction.",
        },
        {
            "id": "M3-inner-both-sides-same-batch",
            "plan": {
                "nodes": [
                    {"id": "s", "op": "source", "types": ["i"]},
                    {"id": "t", "op": "source", "types": ["i"]},
                    {"id": "j", "op": "inner", "left": "s", "right": "t", "on": [[0, 0]]},
                ],
                "root": "j",
            },
            "batches": [
                {"id": "b1", "changes": [["s", "r1", 1, [1]]]},
                {"id": "b2", "changes": [["t", "r1", 1, [1]], ["s", "r2", 1, [1]]]},
            ],
            "reopen_after": [1],
            "locations": ["ivm.py:50", "ivm.py:51"],
            "note": "Both join inputs change in one batch: the da x db cross pair is "
                    "counted twice because both contributions use the new opposite bag.",
        },
        {
            "id": "M4-distinct-support-count",
            "plan": {
                "nodes": [
                    {"id": "s", "op": "source", "types": ["i"]},
                    {"id": "d", "op": "distinct", "input": "s"},
                ],
                "root": "d",
            },
            "batches": [
                {"id": "b1", "changes": [["s", "r1", 1, [1]]]},
                {"id": "b2", "changes": [["s", "r2", 1, [1]]]},
                {"id": "b3", "changes": [["s", "r1", 2, None]]},
            ],
            "reopen_after": [1],
            "locations": ["ivm.py:65", "ivm.py:66"],
            "note": "DISTINCT flips on the sign of each delta instead of a maintained "
                    "support count: support 2 -> 1 wrongly retracts the row.",
        },
        {
            "id": "M5-having-group-state-lost",
            "plan": {
                "nodes": [
                    {"id": "s", "op": "source", "types": ["i", "i"]},
                    {"id": "g", "op": "group", "input": "s", "keys": [0], "value": 1,
                     "having": 0},
                ],
                "root": "g",
            },
            "batches": [
                {"id": "b1", "changes": [["s", "r1", 1, [1, 5]]]},
                {"id": "b2", "changes": [["s", "r2", 1, [1, -10]]]},
                {"id": "b3", "changes": [["s", "r3", 1, [1, 20]]]},
            ],
            "reopen_after": [1],
            "locations": ["ivm.py:16", "ivm.py:29", "ivm.py:30"],
            "note": "HAVING-suppressed groups persist no accumulators: when the group "
                    "becomes visible again SUM/COUNT restart from only the new delta.",
        },
        {
            "id": "M6-global-group-vanishes-on-empty",
            "plan": {
                "nodes": [
                    {"id": "s", "op": "source", "types": ["i"]},
                    {"id": "g", "op": "group", "input": "s", "keys": [], "value": 0,
                     "having": None},
                ],
                "root": "g",
            },
            "batches": [
                {"id": "noop", "changes": []},
                {"id": "b2", "changes": [["s", "r1", 1, [5]]]},
                {"id": "b3", "changes": [["s", "r1", 2, None]]},
            ],
            "reopen_after": [0, 2],
            "locations": ["ivm.py:19", "ivm.py:26", "ivm.py:27"],
            "note": "The keyless global group must always exist as [null,0,0]; when the "
                    "input empties the engine drops it instead of re-emitting it.",
        },
        {
            "id": "M8-left-pad-not-restored-on-right-delete",
            "plan": {
                "nodes": [
                    {"id": "s", "op": "source", "types": ["i"]},
                    {"id": "t", "op": "source", "types": ["i"]},
                    {"id": "lj", "op": "left", "left": "s", "right": "t", "on": [[0, 0]]},
                ],
                "root": "lj",
            },
            "batches": [
                {"id": "b1", "changes": [["t", "r1", 1, [1]]]},
                {"id": "b2", "changes": [["s", "r1", 1, [1]]]},
                {"id": "b3", "changes": [["t", "r1", 2, None]]},
            ],
            "reopen_after": [1],
            "locations": ["ivm.py:51", "rel.py:57", "rel.py:58"],
            "note": "Reverse boundary of M1: deleting the last right match must re-add "
                    "the all-NULL extension; only the matched pair is retracted. The "
                    "matched baseline is built by inserting the left row after the right, "
                    "so no earlier defect corrupts the state under test.",
        },
        {
            "id": "M9-left-same-batch-left-change-right-removal",
            "plan": {
                "nodes": [
                    {"id": "s", "op": "source", "types": ["i"]},
                    {"id": "t", "op": "source", "types": ["i"]},
                    {"id": "lj", "op": "left", "left": "s", "right": "t", "on": [[0, 0]]},
                ],
                "root": "lj",
            },
            "batches": [
                {"id": "b1", "changes": [["t", "r1", 1, [1]]]},
                {"id": "b2", "changes": [["s", "r1", 1, [1]]]},
                {"id": "b3", "changes": [["s", "r2", 1, [1]], ["t", "r1", 2, None]]},
            ],
            "reopen_after": [],
            "locations": ["ivm.py:50", "ivm.py:51", "rel.py:49"],
            "note": "Combined-history defect distinct from M3: a changed left row whose "
                    "right match disappears in the same batch is padded against the new "
                    "bag, losing the old-occurrence padding flip. The matched baseline is "
                    "established by left-after-right inserts so the final simultaneous "
                    "batch is the only batch whose both sides change.",
        },
        {
            "id": "M10-anti-combined-batch",
            "plan": {
                "nodes": [
                    {"id": "s", "op": "source", "types": ["i"]},
                    {"id": "t", "op": "source", "types": ["i"]},
                    {"id": "a", "op": "anti", "left": "s", "right": "t", "on": [[0, 0]]},
                ],
                "root": "a",
            },
            "batches": [
                {"id": "b1", "changes": [["s", "r1", 1, [1]]]},
                {"id": "b2", "changes": [["s", "r2", 1, [1]], ["t", "r1", 1, [1]]]},
            ],
            "reopen_after": [],
            "locations": ["ivm.py:47", "ivm.py:48", "ivm.py:51"],
            "note": "In a combined batch the changed-left existence test itself uses the "
                    "correct new right bag, but the unchanged left occurrence's flip is "
                    "still lost because db is ignored: isolates the two anti causes.",
        },
        {
            "id": "M11-shared-self-join-double-count",
            "plan": {
                "nodes": [
                    {"id": "s", "op": "source", "types": ["i"]},
                    {"id": "t", "op": "source", "types": ["i"]},
                    {"id": "d", "op": "distinct", "input": "t"},
                    {"id": "j", "op": "inner", "left": "s", "right": "d", "on": [[0, 0]]},
                ],
                "root": "j",
            },
            "batches": [
                {"id": "b1", "changes": [["t", "r1", 1, [1]]]},
                {"id": "b2", "changes": [["s", "r1", 1, [1]]]},
                {"id": "b3", "changes": [["s", "r2", 1, [1]]]},
                {"id": "b4", "changes": [["s", "r3", 1, [2]], ["t", "r3", 1, [2]]]},
            ],
            "reopen_after": [1],
            "locations": ["ivm.py:50", "ivm.py:51", "ivm.py:72"],
            "note": "Shared DAG propagation around a join whose right edge is a DISTINCT "
                    "node: single-side batches confirm correct propagation through the "
                    "shared node, then the final both-side batch shows the new cross pair "
                    "double counted because each contribution reads the new opposite bag.",
        },
    ]


def build():
    cases = []
    for spec in _cases():
        plan, batches, reopen_after = spec["plan"], spec["batches"], spec["reopen_after"]
        actual = harness.run(plan, batches, reopen_after)
        histories = source_histories(plan, batches)
        expected = []
        for bags in histories:
            root_bag = evaluate(plan, bags)[0][plan["root"]]
            expected.append(pack(root_bag))
        initial_bag = evaluate(plan, {})[0][plan["root"]]
        expected_deltas = []
        previous = initial_bag
        for rows in expected:
            now = Counter({tuple(r): n for r, n in rows})
            expected_deltas.append(pack(diff(now, previous)))
            previous = now
        receipt_deltas = [receipt["delta"] for receipt in actual["receipts"]]
        mismatch = actual["observed"] != expected or receipt_deltas != expected_deltas
        cases.append({
            "id": spec["id"],
            "plan": plan,
            "batches": batches,
            "reopen_after": reopen_after,
            "expected": expected,
            "observed": actual["observed"],
            "expected_deltas": expected_deltas,
            "receipts": actual["receipts"],
            "status": "mismatch" if mismatch else "control",
            "locations": spec["locations"],
            "note": spec["note"],
        })
    return {"verdict": "block", "cases": cases}


def main():
    report = build()
    with open("review.json", "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2)
    controls = sum(1 for c in report["cases"] if c["status"] == "control")
    mismatches = len(report["cases"]) - controls
    print(f"REVIEW_EVIDENCE_OK cases={len(report['cases'])} "
          f"control={controls} mismatch={mismatches}")


if __name__ == "__main__":
    main()
