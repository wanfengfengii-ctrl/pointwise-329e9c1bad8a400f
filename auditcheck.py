"""Replays submitted review observations. This checker does not certify the design proof."""
import importlib
import json
from pathlib import Path
from rel import pack, unpack, diff, evaluate
from harness import run

def verify():
    module = importlib.import_module("review_evidence")
    fresh = module.build()
    saved = json.loads(Path("review.json").read_text(encoding="utf-8"))
    if fresh != saved:
        raise AssertionError("REVIEW_ARTIFACT_STALE")
    cases = saved["cases"]
    ids = set()
    found_mismatch = False
    found_control = False
    review = Path("REVIEW.md").read_text(encoding="utf-8")
    design = Path("docs/ivm-design.md").read_text(encoding="utf-8")
    for case in cases:
        ident = case["id"]
        assert isinstance(ident, str) and ident and ident not in ids
        ids.add(ident)
        assert ident in review and ident in design
        count = len(case["batches"])
        assert count and len(case["expected"]) == count
        result = run(case["plan"], case["batches"], case["reopen_after"])
        assert case["observed"] == result["observed"]
        assert case["receipts"] == result["receipts"]
        assert case["expected"] == result["reference"]
        initial = evaluate(case["plan"], {})[0][case["plan"]["root"]]
        previous = initial
        expected_deltas = []
        for rows in case["expected"]:
            now = unpack(rows)
            expected_deltas.append(pack(diff(now, previous)))
            previous = now
        assert case["expected_deltas"] == expected_deltas
        mismatch = (case["observed"] != case["expected"] or
                    [r["delta"] for r in case["receipts"]] != expected_deltas)
        assert case["status"] == ("mismatch" if mismatch else "control")
        found_mismatch |= mismatch
        found_control |= not mismatch
        assert case["locations"] and case["note"]
        for location in case["locations"]:
            path, number = location.rsplit(":", 1)
            assert path in ("rel.py", "ivm.py", "store.py", "harness.py")
            assert 1 <= int(number) <= len(Path(path).read_text(encoding="utf-8").splitlines())
    assert found_mismatch and found_control
    assert saved["verdict"] in ("block", "allow")
    print("REVIEW_REPLAY_OK", len(cases))
    return saved

if __name__ == "__main__":
    import sys
    report = verify()
    if sys.argv[1:] == ["demo"]:
        for case in report["cases"]:
            print(json.dumps({"id": case["id"], "status": case["status"],
                              "actual": case["observed"][-1], "expected": case["expected"][-1]},
                             ensure_ascii=False, sort_keys=True))
