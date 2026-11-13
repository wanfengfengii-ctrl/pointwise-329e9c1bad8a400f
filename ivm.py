"""Production incremental transitions. Nodes are processed in stored plan order."""
from collections import Counter
from rel import diff, plus, product, same, scalar

def unary(delta, node):
    op = node["op"]
    if op == "filter":
        return Counter({r: n for r, n in delta.items() if scalar(node["where"], r) is True})
    out = Counter()
    for row, n in delta.items():
        out[tuple(row[i] for i in node["cols"])] += n
    return out

def grouping(old, delta, node):
    size = len(node["keys"])
    groups = {r[:size]: [r[size] or 0, r[size + 1], r[size + 2]] for r in old}
    for row, n in delta.items():
        key = tuple(row[i] for i in node["keys"])
        state = groups.setdefault(key, [0, 0, 0])
        state[1] += n
        if row[node["value"]] is not None:
            state[0] += n * row[node["value"]]
            state[2] += n
    out = Counter()
    for key, (s, count, nonnull) in groups.items():
        if count <= 0:
            continue
        value = s if nonnull else None
        if node["having"] is None or (value is not None and value > node["having"]):
            out[key + (value, count, nonnull)] = 1
    return out

def advance(plan, states, widths, effects):
    for node in plan["nodes"]:
        ident, op = node["id"], node["op"]
        if op == "source":
            continue
        old = states[ident]
        if op in ("inner", "left", "anti"):
            a, b = states[node["left"]], states[node["right"]]
            da = effects.get(node["left"], Counter())
            db = effects.get(node["right"], Counter())
            if not da and not db:
                effects[ident] = Counter()
                continue
            if op == "anti":
                delta = Counter({x: n for x, n in da.items()
                                 if not any(same(x, y, node["on"]) for y in b)})
            else:
                delta = product(da, b, node, widths[node["right"]], op == "left")
                delta.update(product(a, db, node, widths[node["right"]]))
            new = plus(old, delta)
        elif op == "union":
            delta = Counter()
            for child in node["inputs"]:
                delta.update(effects.get(child, Counter()))
            new = plus(old, delta)
        else:
            delta = effects.get(node["input"], Counter())
            if not delta:
                effects[ident] = Counter()
                continue
            if op in ("filter", "project"):
                new = plus(old, unary(delta, node))
            elif op == "distinct":
                new = plus(old, Counter({r: 1 if n > 0 else -1 for r, n in delta.items() if n}))
            elif op == "group":
                new = grouping(old, delta, node)
            else:
                raise ValueError("OP")
        states[ident] = new
        effects[ident] = diff(new, old)
    return states
