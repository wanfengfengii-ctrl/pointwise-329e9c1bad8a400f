"""Bag algebra used for initial materialization and the manual full refresh."""
from collections import Counter

def pack(bag):
    return [[list(row), n] for row, n in sorted(bag.items(), key=lambda p: repr(p[0])) if n]

def unpack(items):
    return Counter({tuple(row): n for row, n in items})

def plus(base, delta):
    out = base.copy()
    for row, n in delta.items():
        out[row] += n
        if out[row] <= 0:
            del out[row]
    return out

def diff(new, old):
    out = Counter()
    for row in new.keys() | old.keys():
        n = new[row] - old[row]
        if n:
            out[row] = n
    return out

def scalar(e, row):
    tag = e[0]
    if tag == "col":
        return row[e[1]]
    if tag == "lit":
        return e[1]
    if tag == "isnull":
        return scalar(e[1], row) is None
    if tag == "not":
        x = scalar(e[1], row)
        return None if x is None else not x
    x, y = scalar(e[1], row), scalar(e[2], row)
    if tag == "and":
        return False if x is False or y is False else (None if x is None or y is None else True)
    if tag == "or":
        return True if x is True or y is True else (None if x is None or y is None else False)
    if x is None or y is None:
        return None
    return x == y if tag == "eq" else x > y

def same(a, b, pairs):
    return all(a[i] is not None and b[j] is not None and a[i] == b[j] for i, j in pairs)

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
    for key, (s, count, nonnull) in groups.items():
        value = s if nonnull else None
        if node["having"] is None or (value is not None and value > node["having"]):
            out[key + (value, count, nonnull)] = 1
    return out

def evaluate(plan, sources):
    states = {}
    widths = {}
    for node in plan["nodes"]:
        ident, op = node["id"], node["op"]
        if op == "source":
            out = sources.get(ident, Counter()).copy()
            width = len(node["types"])
        elif op in ("inner", "left", "anti"):
            a, b = states[node["left"]], states[node["right"]]
            w = widths[node["right"]]
            if op == "anti":
                out = Counter({x: n for x, n in a.items()
                               if not any(same(x, y, node["on"]) for y in b)})
                width = widths[node["left"]]
            else:
                out = product(a, b, node, w, op == "left")
                width = widths[node["left"]] + w
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
