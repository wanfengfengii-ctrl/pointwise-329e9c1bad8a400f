"""Small warehouse plans and recorded batches, kept as importable data."""
PLAN = {
    "nodes": [
        {"id": "readings", "op": "source", "types": ["s", "s", "i"]},
        {"id": "sites", "op": "source", "types": ["s", "s"]},
        {"id": "enriched", "op": "left", "left": "readings", "right": "sites", "on": [[0, 0]]},
        {"id": "daily", "op": "group", "input": "enriched", "keys": [1, 4], "value": 2, "having": None}
    ],
    "root": "daily"
}
NORMAL = [
    {"id": "dimensions", "changes": [["sites", "s1", 1, ["A", "east"]]]},
    {"id": "first", "changes": [["readings", "r1", 1, ["A", "d1", 10]]]},
    {"id": "second", "changes": [["readings", "r2", 1, ["A", "d1", 7]]]},
    {"id": "correct", "changes": [["readings", "r1", 2, ["A", "d1", 12]]]}
]
INCIDENT = [
    {"id": "arrival", "changes": [["readings", "r1", 1, ["A", "d1", 10]]]},
    {"id": "late-dimension", "changes": [["sites", "s1", 1, ["A", "east"]]]}
]
