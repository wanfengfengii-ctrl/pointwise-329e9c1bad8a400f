"""Baseline assertions. Audit evidence extends rather than replaces these checks."""
import json
import sys
from copy import deepcopy
from store import Warehouse
from harness import run, reopen
from rel import pack
from samples import PLAN, NORMAL, INCIDENT

def normal():
    result = run(PLAN, NORMAL, range(len(NORMAL)))
    assert result["observed"] == result["reference"]
    assert result["observed"][-1] == [[["d1", "east", 19, 2, 2], 1]]
    store = Warehouse(plan=PLAN)
    first = store.apply(NORMAL[0])
    store.apply(NORMAL[1])
    snapshot = store.read()
    assert store.apply(NORMAL[0]) == first
    assert store.read() == snapshot
    changed = deepcopy(NORMAL[0])
    changed["changes"][0][3][1] = "west"
    try:
        store.apply(changed)
    except ValueError as error:
        assert str(error) == "REQUEST_CONFLICT"
    else:
        raise AssertionError("missing conflict")
    for point in ("after_sources", "after_views"):
        before = list(store.db.iterdump())
        try:
            store.apply(NORMAL[2], fail_at=point)
        except RuntimeError:
            pass
        else:
            raise AssertionError("missing injection")
        assert list(store.db.iterdump()) == before
        store = reopen(store)
        assert store.read() == snapshot
    store.apply(NORMAL[2])
    before = list(store.db.iterdump())
    conflict = {"id": "conflict", "changes": [
        ["readings", "r1", 1, ["A", "d1", 99]],
        ["readings", "r3", 1, ["A", "d1", 3]]]}
    try:
        store.apply(conflict)
    except ValueError as error:
        assert str(error) == "REVISION_CONFLICT"
    else:
        raise AssertionError("missing revision conflict")
    assert list(store.db.iterdump()) == before
    store.db.close()
    print("NORMAL_REGRESSION_OK")

def incident():
    result = run(PLAN, INCIDENT, [0, 1])
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result["observed"] != result["reference"]:
        print("IVM_OUTER_RETRACTION_MISMATCH")
        return 1
    print("INCIDENT_RECONCILED")
    return 0

if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "normal":
        normal()
    elif mode == "incident":
        raise SystemExit(incident())
    elif mode == "verify":
        normal()
        # A review deliverable keeps the production mismatch observable.
        import auditcheck
        auditcheck.verify()
    else:
        raise SystemExit("unknown check")
