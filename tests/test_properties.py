"""Property-style tests: random owe/ran/close sequences from fixed seeds,
checked against invariants and against a reference model written from the
documented rules rather than from the library's code. Standard library only."""
import os
import random
import shutil
import sys
import tempfile
import unittest
from collections import Counter, defaultdict
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from delegationledger import Ledger  # noqa: E402

SEEDS = range(60)
AS_OF = "2026-10-05T00:00:00Z"
TASKS, AGENTS, STEPS = ["T1", "T2", "T3"], ["a", "b"], ["s1", "s2", "s3", "s4"]
AMOUNTS = [None, "10", "10.0", "10.50", "10.5", "-3", "0", "7"]
DUES = [None, None, "2026-10-01T00:00:00Z", "2026-10-09T00:00:00Z", "2026-10-04T19:00:00-05:00"]


def random_ops(rng, n):
    ops = []
    for _ in range(n):
        r = rng.random()
        t, a, s = rng.choice(TASKS), rng.choice(AGENTS), rng.choice(STEPS)
        if r < 0.45:
            ops.append(("owe", t, a, s, rng.choice(AMOUNTS), rng.choice(DUES)))
        elif r < 0.92:
            ops.append(("ran", t, a, s, rng.choice(AMOUNTS), None))
        else:
            ops.append(("close", t, None, None, None, None))
    return ops


def apply(led, ops):
    for book, t, a, s, amt, due in ops:
        if book == "owe":
            led.owe(t, a, s, amount=amt, due=due)
        elif book == "ran":
            led.ran(t, a, s, amount=amt)
        else:
            led.close(t)


def reference(ops, as_of):
    """The documented rules, written out plainly over the op list."""
    from datetime import datetime
    as_of_dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    owed, ran, owe_pos, close_pos, first_owed = defaultdict(list), defaultdict(list), defaultdict(list), {}, {}
    for i, (book, t, a, s, amt, due) in enumerate(ops):
        if book == "owe":
            owed[(t, a, s)].append((amt, due))
            owe_pos[(t, a, s)].append(i)
            first_owed.setdefault((t, a), [])
            if s not in first_owed[(t, a)]:
                first_owed[(t, a)].append(s)
        elif book == "ran":
            ran[(t, a, s)].append(amt)
        else:
            close_pos[t] = i
    out = {}
    for key in set(owed) | set(ran):
        t, a, s = key
        o, r = owed.get(key, []), ran.get(key, [])
        if len(r) > len(o):
            out[key] = "unowed"
        elif len(r) < len(o):
            closed = close_pos.get(t, -1) > max(owe_pos[key])
            order = first_owed[(t, a)]
            later = order[order.index(s) + 1:]
            moved = any(ran.get((t, a, x)) for x in later)
            dues = [datetime.fromisoformat(d.replace("Z", "+00:00")) for _, d in o if d]
            overdue = bool(dues) and min(dues) < as_of_dt
            out[key] = "skipped" if (closed or moved or overdue) else "open"
        elif any(x is not None and (y is None or Decimal(x) != Decimal(y)) for (x, _), y in zip(o, r)):
            out[key] = "amount"
        else:
            out[key] = "ok"
    return out


class Properties(unittest.TestCase):
    def test_matches_the_reference_model(self):
        for seed in SEEDS:
            rng = random.Random(seed)
            ops = random_ops(rng, rng.randint(0, 80))
            led = Ledger()
            apply(led, ops)
            got = {(r.task_id, r.agent, r.step): r.status for r in led.rows(AS_OF)}
            self.assertEqual(got, reference(ops, AS_OF), f"seed {seed}")

    def test_every_ran_without_an_owe_is_unowed(self):
        for seed in SEEDS:
            rng = random.Random(1000 + seed)
            ops = random_ops(rng, 60)
            led = Ledger()
            apply(led, ops)
            owed_keys = {(t, a, s) for b, t, a, s, _, _ in ops if b == "owe"}
            for r in led.rows(AS_OF):
                if (r.task_id, r.agent, r.step) not in owed_keys:
                    self.assertEqual((r.owed, r.status), (0, "unowed"), f"seed {seed}")

    def test_owed_and_run_with_equal_amounts_balances(self):
        for seed in SEEDS:
            rng = random.Random(2000 + seed)
            led = Ledger()
            plan = []
            for t in TASKS:
                for a in AGENTS:
                    for s in rng.sample(STEPS, rng.randint(1, len(STEPS))):
                        for _ in range(rng.randint(1, 3)):
                            plan.append((t, a, s, rng.choice(AMOUNTS)))
            for t, a, s, amt in plan:
                led.owe(t, a, s, amount=amt, due=rng.choice(DUES))
            # run each at an equal amount, possibly written differently, in a shuffled order
            # that keeps each account's own order so amounts pair up one to one
            spell = {"10": "10.00", "10.5": "10.50", "10.50": "10.5", "10.0": "10", "-3": "-3.0", "0": "0.00", "7": "7"}
            runs = list(plan)
            rng.shuffle(runs)
            per_key = defaultdict(list)
            for t, a, s, amt in plan:
                per_key[(t, a, s)].append(amt)
            for t, a, s, _ in runs:
                amt = per_key[(t, a, s)].pop(0)
                led.ran(t, a, s, amount=spell.get(amt, amt) if amt is not None else None)
            if rng.random() < 0.5:
                led.close(rng.choice(TASKS))
            b = led.balance(AS_OF)
            self.assertTrue(b.balanced, f"seed {seed}: {b.to_dict()}")
            self.assertTrue(all(r.status == "ok" for r in led.rows(AS_OF)))

    def test_closing_marks_exactly_the_still_owed_steps(self):
        for seed in SEEDS:
            rng = random.Random(3000 + seed)
            ops = [op for op in random_ops(rng, 70) if op[0] != "close"]
            led = Ledger()
            apply(led, ops)
            before = {(r.task_id, r.agent, r.step): r for r in led.rows(AS_OF)}
            task = rng.choice(TASKS)
            led.close(task)
            after = {(r.task_id, r.agent, r.step): r for r in led.rows(AS_OF)}
            self.assertEqual(set(before), set(after))
            for key, r in after.items():
                was = before[key]
                if key[0] == task and was.ran < was.owed:
                    self.assertEqual((r.status, r.reason), ("skipped", "closed"), f"seed {seed} {key}")
                else:
                    self.assertEqual((r.status, r.reason), (was.status, was.reason), f"seed {seed} {key}")

    def test_counts_are_conserved_and_statuses_partition(self):
        for seed in SEEDS:
            rng = random.Random(4000 + seed)
            ops = random_ops(rng, 90)
            led = Ledger()
            apply(led, ops)
            rows = led.rows(AS_OF)
            n = Counter(op[0] for op in ops)
            self.assertEqual(sum(r.owed for r in rows), n["owe"])
            self.assertEqual(sum(r.ran for r in rows), n["ran"])
            b = led.balance(AS_OF)
            listed = len(b.open_obligations) + len(b.skipped_steps) + len(b.amount_mismatches) + len(b.unowed_runs)
            self.assertEqual(listed, sum(1 for r in rows if r.status != "ok"))
            self.assertEqual(len({(r.task_id, r.agent, r.step) for r in rows}), len(rows))
            for r in rows:
                self.assertIn(r.status, ("ok", "open", "skipped", "amount", "unowed"))
                self.assertEqual(r.reason is not None, r.status == "skipped")

    def test_balance_survives_the_file_and_rotation(self):
        d = tempfile.mkdtemp()
        try:
            for seed in range(15):
                rng = random.Random(5000 + seed)
                ops = random_ops(rng, 50)
                mem = Ledger(clock=lambda: "2026-10-01T00:00:00Z")
                apply(mem, ops)
                path = os.path.join(d, f"p{seed}.jsonl")
                disk = Ledger(path, clock=lambda: "2026-10-01T00:00:00Z")
                for i, op in enumerate(ops):
                    apply(disk, [op])
                    if rng.random() < 0.1:
                        disk.rotate()
                again = Ledger(path)
                self.assertTrue(again.verify())
                self.assertEqual(again.trial_balance(AS_OF), mem.trial_balance(AS_OF), f"seed {seed}")
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
