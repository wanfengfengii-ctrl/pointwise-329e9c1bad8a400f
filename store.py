"""Revisioned source tables, materialized bags and request receipts in one SQLite transaction."""
import json
import sqlite3
from collections import Counter
import ivm
from rel import pack, unpack, diff, evaluate

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)

class Warehouse:
    def __init__(self, db=":memory:", plan=None):
        self.db = db if isinstance(db, sqlite3.Connection) else sqlite3.connect(db)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS records(src TEXT, rid TEXT, rev INTEGER, body TEXT NOT NULL,
                                          PRIMARY KEY(src,rid));
        CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, request TEXT NOT NULL, response TEXT NOT NULL);
        """)
        found = self.db.execute("SELECT v FROM meta WHERE k='plan'").fetchone()
        if found is None:
            if plan is None:
                raise ValueError("PLAN_REQUIRED")
            # Plans are trusted, schema-checked deployment artifacts.
            states, widths = evaluate(plan, {})
            with self.db:
                self.db.execute("INSERT INTO meta VALUES('plan',?)", (canonical(plan),))
                self.db.execute("INSERT INTO meta VALUES('generation','0')")
                for ident, bag in states.items():
                    self.db.execute("INSERT INTO nodes VALUES(?,?)", (ident, canonical(pack(bag))))
            self.plan = plan
        else:
            self.plan = json.loads(found[0])
            if plan is not None and canonical(plan) != found[0]:
                raise ValueError("PLAN_CONFLICT")
        _, self.widths = evaluate(self.plan, {})
        self.sources = {n["id"]: n["types"] for n in self.plan["nodes"] if n["op"] == "source"}

    def _states(self):
        return {i: unpack(json.loads(body)) for i, body in self.db.execute("SELECT id,body FROM nodes")}

    def read(self):
        bag = self._states()[self.plan["root"]]
        generation = int(self.db.execute("SELECT v FROM meta WHERE k='generation'").fetchone()[0])
        return {"generation": generation, "rows": pack(bag)}

    def raw_sources(self):
        bags = {s: Counter() for s in self.sources}
        for src, body in self.db.execute("SELECT src,body FROM records"):
            row = json.loads(body)
            if row is not None:
                bags[src][tuple(row)] += 1
        return bags

    def _validate(self, changes):
        seen = set()
        for item in changes:
            if not isinstance(item, list) or len(item) != 4:
                raise ValueError("INPUT")
            src, rid, rev, row = item
            if not isinstance(src, str) or src not in self.sources or not isinstance(rid, str) or not rid:
                raise ValueError("INPUT")
            if type(rev) is not int or not 0 < rev < 2**63 or (src, rid) in seen:
                raise ValueError("INPUT")
            seen.add((src, rid))
            if row is None:
                continue
            types = self.sources[src]
            if not isinstance(row, list) or len(row) != len(types):
                raise ValueError("INPUT")
            for value, typ in zip(row, types):
                if value is not None and ((typ == "i" and type(value) is not int)
                                          or (typ == "s" and type(value) is not str)):
                    raise ValueError("INPUT")

    def apply(self, batch, fail_at=None):
        ident, changes = batch["id"], batch["changes"]
        if not isinstance(ident, str) or not ident:
            raise ValueError("INPUT")
        request = canonical(changes)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            receipt = self.db.execute("SELECT request,response FROM receipts WHERE id=?", (ident,)).fetchone()
            if receipt:
                if receipt[0] != request:
                    raise ValueError("REQUEST_CONFLICT")
                self.db.commit()
                return json.loads(receipt[1])
            self._validate(changes)
            states = self._states()
            old_root = states[self.plan["root"]].copy()
            before = {s: states[s].copy() for s in self.sources}
            for src, rid, rev, row in changes:
                prior = self.db.execute("SELECT rev,body FROM records WHERE src=? AND rid=?", (src, rid)).fetchone()
                body = canonical(row)
                if prior and rev <= prior[0]:
                    if rev == prior[0] and body != prior[1]:
                        raise ValueError("REVISION_CONFLICT")
                    continue
                if prior:
                    old_row = json.loads(prior[1])
                    if old_row is not None:
                        key = tuple(old_row)
                        states[src][key] -= 1
                        if states[src][key] == 0:
                            del states[src][key]
                if row is not None:
                    states[src][tuple(row)] += 1
                self.db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?,?)", (src, rid, rev, body))
            if fail_at == "after_sources":
                raise RuntimeError("INJECTED_AFTER_SOURCES")
            effects = {s: diff(states[s], before[s]) for s in self.sources}
            ivm.advance(self.plan, states, self.widths, effects)
            for node_id, bag in states.items():
                self.db.execute("UPDATE nodes SET body=? WHERE id=?", (canonical(pack(bag)), node_id))
            if fail_at == "after_views":
                raise RuntimeError("INJECTED_AFTER_VIEWS")
            generation = int(self.db.execute("SELECT v FROM meta WHERE k='generation'").fetchone()[0]) + 1
            self.db.execute("UPDATE meta SET v=? WHERE k='generation'", (str(generation),))
            response = {"generation": generation, "delta": pack(diff(states[self.plan["root"]], old_root))}
            self.db.execute("INSERT INTO receipts VALUES(?,?,?)", (ident, request, canonical(response)))
            self.db.commit()
            return response
        except BaseException:
            self.db.rollback()
            raise
