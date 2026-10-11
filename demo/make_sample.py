#!/usr/bin/env python3
"""Generate demo/sample-ledger.jsonl and its companions.

A simulated procurement run in the shape of the Trial Balance campaign's
workflow: a buyer agent owes the first five steps of each requisition, a
payables agent owes the last two. Each of the campaign's five fault conditions
appears once, plus clean requisitions, one still in progress and one run nobody
owed. Simulation from published coefficients, not a deployed system: every
amount and time below is made up for the demo and none is a measurement.

Writes:
  sample-ledger.jsonl   the ledger itself (hash-chained)
  sample-truth.json     what the generator injected, so the page can show what the balance missed
  sample-ledger.js      both of the above inlined, for opening index.html from disk,
                        plus the fixtures the page's console self-check runs against
  fixtures/             the same fixtures as files, to drop on the page by hand
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from delegationledger import Ledger  # noqa: E402

BUYER_STEPS = ["check_vendor", "get_quote", "create_po", "request_approval", "post_goods_receipt"]
PAYABLES_STEPS = ["post_invoice", "schedule_payment"]
AMOUNT_STEPS = {"create_po", "post_invoice", "schedule_payment"}
APPROVAL_THRESHOLD = 5000
DUE = "2026-10-01T17:00:00Z"
CHECK_AS_OF = "2026-10-09T00:00:00Z"

# (task, true amount, condition, detail)
PLAN = [
    ("R-0101", 1840, "N", None),
    ("R-0102", 7320, "O1", "check_vendor"),
    ("R-0103", 3275, "N", None),
    ("R-0104", 12480, "O2", "request_approval"),
    ("R-0105", 5955, "C1", "post_invoice"),
    ("R-0106", 9160, "N", None),
    ("R-0107", 6418, "C2", None),
    ("R-0108", 2210, "EXTRA", "expedite_shipping"),
    ("R-0109", 15200, "OPEN", None),
    ("R-0110", 4380, "CLOSED", None),
]

LABELS = {
    "N": "no fault",
    "O1": "skipped step",
    "O2": "skipped step, diary claims it was done",
    "C1": "wrong amount",
    "C2": "wrong amount on both sides",
    "EXTRA": "a step nobody owed",
    "OPEN": "in progress, not late",
    "CLOSED": "closed by the commissioner before it finished",
}


def transpose(amount):
    d = str(amount)
    for i in range(len(d) - 1):
        if d[i] != d[i + 1]:
            t = d[:i] + d[i + 1] + d[i] + d[i + 2:]
            if t[0] != "0":
                return int(t)
    return amount * 10


def clock():
    t = [datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)]

    def tick():
        t[0] += timedelta(seconds=7)
        return t[0].isoformat().replace("+00:00", "Z")
    return tick


def owed_steps(amount):
    for agent, steps in (("buyer", BUYER_STEPS), ("payables", PAYABLES_STEPS)):
        for s in steps:
            if s == "request_approval" and amount <= APPROVAL_THRESHOLD:
                continue
            yield agent, s, (amount if s in AMOUNT_STEPS else None)


def build(path):
    if os.path.exists(path):
        os.remove(path)
    tick = clock()
    com = Ledger.open(path, "commissioner", clock=tick)   # lives in the dispatcher
    har = Ledger.open(path, "harness", clock=tick)        # lives where tools run
    truth = []
    for task, amount, cond, detail in PLAN:
        assigned = transpose(amount) if cond == "C2" else amount
        due = None if cond in ("OPEN", "CLOSED") else DUE
        plan = list(owed_steps(assigned))
        for agent, step, amt in plan:                         # commissioning, before anyone acts
            com.owe(task, agent, step, amount=amt, due=due)
        done = plan[:3] if cond == "OPEN" else plan[:2] if cond == "CLOSED" else plan
        diary = []
        for agent, step, amt in done:                         # the harness records what ran
            if cond in ("O1", "O2") and step == detail:
                if cond == "O2":
                    diary.append(f"{task}: {step} done.")
                continue
            posted = transpose(amt) if (cond == "C1" and step == detail) else amt
            har.ran(task, agent, step, amount=posted)
            if cond == "EXTRA" and step == "create_po":
                har.ran(task, "buyer", detail)
        if cond == "CLOSED":
            com.close(task)
        t = {"task_id": task, "condition": cond, "label": LABELS[cond], "true_amount": str(amount)}
        if cond in ("O1", "O2"):
            t["step"] = detail
        if cond == "O2":
            t["diary_claim"] = diary[0]
        if cond == "C1":
            t["step"] = detail
            t["posted"] = str(transpose(amount))
        if cond == "C2":
            t["assigned_and_posted"] = str(assigned)
        if cond == "EXTRA":
            t["step"] = detail
        truth.append(t)
    return Ledger(path), truth


def _keys(rows):
    return sorted(f"{r.task_id}/{r.agent}/{r.step}:{r.status}:{r.reason or ''}" for r in rows)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def fixtures(tmp):
    """Ledgers the page checks itself against: Python writes them and records
    what the balance and the chain must come to."""
    import random
    out = {}

    # 1. random owe/ran/close sequence: the JavaScript balance must equal Python's
    rng = random.Random(7)
    path = os.path.join(tmp, "property.jsonl")
    led = Ledger(path, clock=clock())
    dues = [None, None, "2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z", "2026-10-04T19:00:00-05:00"]
    amounts = [None, "10", "10.0", "10.50", "10.5", "-3", "0", "7", "1e3", "1000"]
    for _ in range(300):
        r = rng.random()
        t, a, st = rng.choice(["T1", "T2", "T3", "T4"]), rng.choice(["a", "b"]), rng.choice(["s1", "s2", "s3", "s4"])
        if r < 0.45:
            led.owe(t, a, st, amount=rng.choice(amounts), due=rng.choice(dues))
        elif r < 0.93:
            led.ran(t, a, st, amount=rng.choice(amounts))
        else:
            led.close(t)
    out["property"] = {"raw": _read(path), "as_of": "2026-10-05T00:00:00Z",
                       "rows": _keys(led.rows("2026-10-05T00:00:00Z")), "head": led.head}

    # 2. a series rotated twice, handed to the page out of order
    path = os.path.join(tmp, "series.jsonl")
    led = Ledger(path, clock=clock())
    led.owe("R-1", "buyer", "create_po", amount=500)
    led.owe("R-1", "buyer", "post_goods_receipt")
    seal1 = led.rotate()
    led.ran("R-1", "buyer", "create_po", amount=500)
    led.owe("R-2", "buyer", "create_po", amount=75)
    seal2 = led.rotate()
    led.ran("R-1", "buyer", "post_goods_receipt")
    led.ran("R-2", "buyer", "create_po", amount=57)
    names = led.segments + [os.path.basename(path)]
    segs = [{"name": n, "raw": _read(os.path.join(tmp, n))} for n in names]
    out["rotated"] = {"segments": [segs[2], segs[0], segs[1]], "head": led.head,
                      "seals": [seal1["head"], seal2["head"]], "as_of": "2026-10-05T00:00:00Z",
                      "rows": _keys(led.rows("2026-10-05T00:00:00Z"))}

    # 3. Unicode in every name: the browser must hash the same bytes
    path = os.path.join(tmp, "unicode.jsonl")
    led = Ledger(path, clock=clock())
    for n in ["R-\u2028-sep", "R-\x85-nel", "\u0645\u0647\u0645\u0629", "e\u0301", "\U0001F9FE", "tab\there",
              "quote\"back\\slash", "\x1cx\x1d", "\u00a0", "\x7f", "\u202eevil", "\ufeffbom"]:
        led.owe(n, n, n, amount=1, by=n)
        led.ran(n, n, n, amount="1.0", by=n)
    out["unicode"] = {"raw": _read(path), "head": led.head, "entries": len(led.entries)}

    # 4. markup in every text field, chain valid; and a forged amount with a valid hash
    xss = ['<script>document.documentElement.dataset.pwned="script"</script>',
           '<img src=x onerror="document.documentElement.dataset.pwned=\'img\'">']
    path = os.path.join(tmp, "hostile.jsonl")
    led = Ledger(path, clock=lambda: xss[1])
    for x in xss:
        led.owe(x, x, x, amount=5, due="2026-10-01T00:00:00Z", by=x)
        led.ran(x, x, x + "-ran", amount=6, by=x)
        led.close(x, by=x)
    out["hostile"] = {"raw": _read(path), "payloads": xss}
    from delegationledger.ledger import FORMAT, GENESIS, canonical, entry_hash
    body = {"fmt": FORMAT, "book": "owe", "role": "commissioner", "task_id": "T", "agent": "a", "step": "s",
            "amount": xss[1], "due": None, "by": "x", "ts": "2026-10-01T00:00:00Z", "seq": 1, "prev": GENESIS}
    out["forged"] = {"raw": canonical(dict(body, hash=entry_hash(body))) + "\n"}
    return out


def main():
    import shutil
    import tempfile
    jsonl = os.path.join(HERE, "sample-ledger.jsonl")
    led, truth = build(jsonl)
    bal = led.balance(as_of=CHECK_AS_OF)
    expected = {
        "as_of": CHECK_AS_OF,
        "skipped": sorted(f"{x['task_id']}/{x['agent']}/{x['step']}" for x in bal.skipped_steps),
        "amount": sorted(f"{x['task_id']}/{x['agent']}/{x['step']}" for x in bal.amount_mismatches),
        "unowed": sorted(f"{x['task_id']}/{x['agent']}/{x['step']}" for x in bal.unowed_runs),
        "open": sorted(f"{x['task_id']}/{x['agent']}/{x['step']}" for x in bal.open_obligations),
        "head": led.head,
        "entries": len(led.entries),
    }
    doc = {"generator": "demo/make_sample.py",
           "note": "Simulation from published coefficients, not a deployed system. Amounts and times are made up.",
           "requisitions": truth, "expected_balance": expected}
    with open(os.path.join(HERE, "sample-truth.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    with open(jsonl, encoding="utf-8") as fh:
        raw = fh.read()
    tmp = tempfile.mkdtemp()
    try:
        fx = fixtures(tmp)
    finally:
        shutil.rmtree(tmp)
    fdir = os.path.join(HERE, "fixtures")
    os.makedirs(fdir, exist_ok=True)
    for name in ("hostile", "unicode", "property"):
        with open(os.path.join(fdir, f"{name}.jsonl"), "w", encoding="utf-8", newline="") as fh:
            fh.write(fx[name]["raw"])
    for seg in fx["rotated"]["segments"]:
        with open(os.path.join(fdir, seg["name"]), "w", encoding="utf-8", newline="") as fh:
            fh.write(seg["raw"])
    with open(os.path.join(HERE, "sample-ledger.js"), "w", encoding="utf-8") as fh:
        fh.write("// generated by make_sample.py; lets index.html run from file:// where fetch is blocked\n")
        fh.write("window.SAMPLE_LEDGER = " + json.dumps(raw) + ";\n")
        fh.write("window.SAMPLE_TRUTH = " + json.dumps(doc) + ";\n")
        fh.write("window.FIXTURES = " + json.dumps(fx) + ";\n")
    print(f"{len(led.entries)} entries; skipped {len(bal.skipped_steps)}, amount {len(bal.amount_mismatches)}, "
          f"unowed {len(bal.unowed_runs)}, open {len(bal.open_obligations)}; head {led.head[:12]}")


if __name__ == "__main__":
    main()
