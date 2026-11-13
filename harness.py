"""Public replay driver; reconstructs objects over a standalone SQLite backup."""
import json
import sqlite3
from store import Warehouse
from rel import pack, evaluate

def reopen(store):
    conn = sqlite3.connect(":memory:")
    store.db.backup(conn)
    store.db.close()
    return Warehouse(conn)

def run(plan, batches, reopen_after=()):
    store = Warehouse(plan=plan)
    observed, reference, receipts = [], [], []
    for index, batch in enumerate(batches):
        receipts.append(store.apply(batch))
        observed.append(store.read()["rows"])
        expected = pack(evaluate(plan, store.raw_sources())[0][plan["root"]])
        reference.append(expected)
        if index in reopen_after:
            store = reopen(store)
    store.db.close()
    return {"observed": observed, "reference": reference, "receipts": receipts}

def main():
    import argparse
    import samples
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["normal", "incident", "file"])
    parser.add_argument("path", nargs="?")
    args = parser.parse_args()
    if args.mode == "file":
        with open(args.path, encoding="utf-8") as stream:
            case = json.load(stream)
        result = run(case["plan"], case["batches"], case.get("reopen_after", []))
    else:
        batches = samples.NORMAL if args.mode == "normal" else samples.INCIDENT
        result = run(samples.PLAN, batches, range(len(batches)))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))

if __name__ == "__main__":
    main()
