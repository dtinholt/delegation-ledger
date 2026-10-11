"""The HTML report's views, the CSV export, watch mode, the CLI's newer
commands, the adapters' error paths and the agent-loop example."""
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from delegationledger import (Ledger, LedgerError, by_agent, by_task, commission, observed, render,  # noqa: E402
                              timeline, watch, write_csv)
from delegationledger.cli import main as cli  # noqa: E402
from delegationledger.report import COLUMN_NOTES, TIMELINE_CAP, timeline_svg  # noqa: E402

AS_OF = "2026-10-05T00:00:00Z"


def tick():
    t = [0]

    def clock():
        t[0] += 1
        return f"2026-10-01T09:{t[0] // 60:02d}:{t[0] % 60:02d}Z"
    return clock


def sample(led):
    for s, amt in (("check", None), ("pay", 100), ("file", None)):
        led.owe("R-1", "buyer", s, amount=amt)
    led.ran("R-1", "buyer", "check")
    led.ran("R-1", "buyer", "pay", amount=101)
    led.ran("R-1", "buyer", "file")
    led.owe("R-2", "payables", "invoice", amount=50)
    led.owe("R-2", "payables", "schedule")
    led.ran("R-2", "payables", "schedule")
    led.ran("R-2", "buyer", "expedite")
    led.owe("R-3", "buyer", "wait")
    led.close("R-3")
    return led


class Views(unittest.TestCase):
    def setUp(self):
        self.led = sample(Ledger(clock=tick()))
        self.rows = self.led.trial_balance(AS_OF)

    def test_by_agent(self):
        agents = {a["agent"]: a for a in by_agent(self.rows)}
        self.assertEqual(set(agents), {"buyer", "payables"})
        self.assertEqual((agents["buyer"]["lines"], agents["buyer"]["amount"], agents["buyer"]["unowed"],
                          agents["buyer"]["skipped"], agents["buyer"]["tasks"]), (5, 1, 1, 1, 3))
        self.assertEqual(sum(a["lines"] for a in agents.values()), len(self.rows))
        self.assertEqual(sum(a["owed"] for a in agents.values()), sum(r["owed"] for r in self.rows))

    def test_by_task(self):
        tasks = {t["task_id"]: t for t in by_task(self.rows, self.led.entries)}
        self.assertEqual(tasks["R-2"]["agents"], ["buyer", "payables"])
        self.assertTrue(tasks["R-3"]["closed"])
        self.assertFalse(tasks["R-1"]["closed"])
        self.assertEqual(tasks["R-2"]["skipped"], 1)  # invoice: schedule, owed after it, ran
        self.assertLess(tasks["R-1"]["first_ts"], tasks["R-1"]["last_ts"])

    def test_timeline(self):
        ev = timeline(self.led.entries, "R-3")
        self.assertEqual([e["book"] for e in ev], ["owe", "close"])
        self.assertEqual(len(timeline(self.led.entries, "R-1")), 6)
        svg = timeline_svg("R-1", timeline(self.led.entries, "R-1"), {("buyer", "pay")})
        self.assertEqual(svg.count("<rect"), 2)          # owed and in balance: squares
        self.assertEqual(svg.count("<polygon"), 1)       # owed and out of balance: a diamond, a different shape
        self.assertIn('r="5.2"', svg)                    # the out-of-balance run is a larger circle
        self.assertEqual(svg.count("<circle"), 3)
        self.assertEqual(svg.count(' bad"'), 2)       # the owed square and the ran circle of pay
        self.assertIn('aria-label="R-1: 3 owe lines, 3 ran lines, 1 steps out of balance, spaced by time"', svg)

    def test_timeline_falls_back_to_file_order(self):
        led = Ledger(clock=lambda: "not a time")
        led.owe("T", "a", "s")
        led.ran("T", "a", "s")
        self.assertIn("spaced in file order", timeline_svg("T", timeline(led.entries, "T"), set()))
        one = Ledger(clock=lambda: "2026-10-01T00:00:00Z")
        one.owe("T", "a", "s")
        self.assertIn("<rect", timeline_svg("T", timeline(one.entries, "T"), set()))

    def test_report_sections(self):
        page = render(self.led, AS_OF, title="R & D <report>")
        for heading in ("Imbalances", "Trial balance", "By agent", "By task", "Timeline per task", "Segments",
                        "What each column means", "What this report cannot see"):
            self.assertIn(f"<h2>{heading}</h2>", page)
        self.assertIn("R &amp; D &lt;report&gt;", page)
        self.assertIn("0 of 1,044", page)
        self.assertIn("Dinand Tinholt · Research", page)
        self.assertIn("https://dinand.com/research/", page)
        for col in COLUMN_NOTES:
            self.assertIn(f"<dt>{col}</dt>", page)
        self.assertIn("One file, never rotated.", page)
        self.assertIn("CHAIN VERIFIED", page)

    def test_report_tables_have_captions_and_scope(self):
        page = render(self.led, AS_OF)
        self.assertEqual(page.count("<caption>"), 3)
        self.assertNotIn("<th>", page)               # every header cell carries a scope
        self.assertIn('<th scope="row">buyer</th>', page)
        self.assertIn(".owe.bad{fill:none;stroke-width:2.6}", page)

    def test_report_caps_timelines(self):
        led = Ledger()
        for i in range(TIMELINE_CAP + 5):
            led.owe(f"T{i:03d}", "a", "s")
        led.ran("T064", "a", "x")   # the one imbalance sits at the end and is drawn first
        page = render(led, AS_OF)
        self.assertEqual(page.count('<div class="tl">'), TIMELINE_CAP)
        self.assertIn("5 more tasks not drawn", page)
        self.assertLess(page.index("T064 · "), page.index("T000 · "))

    def test_report_on_a_rotated_file(self):
        d = tempfile.mkdtemp()
        try:
            led = sample(Ledger(os.path.join(d, "l.jsonl"), clock=tick()))
            led.rotate()
            led.ran("R-3", "buyer", "wait")
            page = render(led, AS_OF)
            self.assertIn("l.000001.jsonl: 13 lines", page)
            self.assertIn("2 FILE(S)", page)
            with open(os.path.join(d, "l.000001.jsonl"), "a") as fh:
                fh.write("tampered\n")
            # the loaded object still renders, and says the re-read failed
            page = render(led, AS_OF)
            self.assertIn("CHAIN BROKEN ON RE-READ", page)
            self.assertIn("could not be re-read", page)
        finally:
            shutil.rmtree(d)


class Export(unittest.TestCase):
    def test_csv(self):
        led = sample(Ledger(clock=tick()))
        led.owe("=HYPERLINK(\"x\")", "+agent", "-step", amount=-4)
        buf = io.StringIO()
        n = write_csv(led, buf, AS_OF)
        rows = list(csv.reader(io.StringIO(buf.getvalue())))
        self.assertEqual(rows[0], ["task_id", "agent", "step", "owed", "ran", "owed_amount", "ran_amount",
                                   "status", "reason", "due"])
        self.assertEqual(n, len(rows) - 1)
        pay = [r for r in rows if r[2] == "pay"][0]
        self.assertEqual(pay[5:8], ["100", "101", "amount"])
        hostile = [r for r in rows if "HYPERLINK" in r[0]][0]
        self.assertEqual(hostile[:3], ["'=HYPERLINK(\"x\")", "'+agent", "'-step"])
        self.assertEqual(hostile[5], "-4")   # amounts stay numbers
        wait = [r for r in rows if r[2] == "wait"][0]
        self.assertEqual(wait[7:9], ["skipped", "closed"])


class Watch(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "w.jsonl")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_prints_new_and_cleared_imbalances_once(self):
        led = Ledger(self.path)
        led.owe("T", "a", "pay", amount=5)
        led.ran("T", "a", "pay", amount=6)
        out = io.StringIO()
        script = iter([
            lambda: led.ran("T", "a", "extra"),           # poll 2: one new unowed
            lambda: None,                                 # poll 3: nothing new
            lambda: led.ran("T", "a", "pay", amount=6),   # poll 4: pay ran twice, owed once: amount clears, unowed appears
        ])
        printed = watch(self.path, interval=0, iterations=4, out=out, as_of=AS_OF,
                        sleep=lambda s: next(script)(), now=lambda: "NOW")
        text = out.getvalue().splitlines()
        self.assertEqual(text[0].split()[1:3], ["now", "amount"])
        self.assertEqual(sum("new" in x.split()[1] for x in text), 2)
        self.assertTrue(any(x.split()[1:3] == ["cleared", "amount"] for x in text))
        self.assertEqual(printed, 3)

    def test_quiet_start_and_chain_break(self):
        Ledger(self.path).owe("T", "a", "s")
        out = io.StringIO()

        def tamper(_):
            with open(self.path, "a") as fh:
                fh.write("{broken}\n")
        printed = watch(self.path, interval=0, iterations=3, out=out, as_of=AS_OF, sleep=tamper, now=lambda: "NOW")
        text = out.getvalue()
        self.assertIn("nothing out of balance", text)
        self.assertEqual(text.count("CHAIN"), 1)     # reported once, not every poll
        self.assertEqual(printed, 0)

    def test_recovers_after_repair(self):
        led = Ledger(self.path)
        led.owe("T", "a", "s")
        with open(self.path, "rb") as fh:
            good = fh.read()
        out = io.StringIO()
        def write(mode, data):
            with open(self.path, mode) as fh:
                fh.write(data)
        steps = iter([lambda: write("ab", b"x"), lambda: write("wb", good)])

        def step(_):
            next(steps)()
        watch(self.path, interval=0, iterations=3, out=out, as_of=AS_OF, sleep=step, now=lambda: "NOW")
        self.assertIn("chain readable again", out.getvalue())

    def test_hostile_names_cannot_reach_the_terminal(self):
        led = Ledger(self.path)
        led.ran("T\x1b[2J", "a", "s\x07")
        out = io.StringIO()
        watch(self.path, iterations=1, out=out, as_of=AS_OF, now=lambda: "NOW")
        self.assertNotIn("\x1b", out.getvalue())
        self.assertNotIn("\x07", out.getvalue())
        self.assertIn("\\x1b[2J", out.getvalue())


class CLI(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "c.jsonl")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli(["--ledger", self.path, *args])
        return code, out.getvalue(), err.getvalue()

    def test_rotate_verify_export_watch(self):
        self.assertEqual(self.run_cli("rotate")[0], 2)            # no ledger yet
        self.run_cli("owe", "R-1", "buyer", "pay", "--amount", "10", "--due", "2026-10-01T12:00:00-05:00")
        code, out, _ = self.run_cli("rotate")
        self.assertEqual(code, 0)
        self.assertIn("sealed c.000001.jsonl", out)
        self.run_cli("ran", "R-1", "buyer", "pay", "--amount", "10.0")
        code, out, _ = self.run_cli("verify")
        self.assertEqual(code, 0)
        self.assertIn("c.000001.jsonl: 2 lines", out)
        self.assertIn("in 2 file(s)", out)
        csv_path = os.path.join(self.dir, "tb.csv")
        code, out, _ = self.run_cli("export", "--csv", csv_path)
        self.assertEqual((code, out.strip()), (0, f"wrote 1 rows to {csv_path}"))
        with open(csv_path) as fh:
            self.assertIn("R-1,buyer,pay,1,1,10,10.0,ok", fh.read())
        code, out, _ = self.run_cli("export", "--csv", "-")
        self.assertTrue(out.startswith("task_id,agent,step"))
        code, out, _ = self.run_cli("watch", "--interval", "0.01", "--count", "1")
        self.assertEqual(code, 0)
        self.assertIn("nothing out of balance", out)
        self.assertEqual(self.run_cli("watch", "--interval", "0")[0], 3)
        code, out, _ = self.run_cli("balance", "--json")
        self.assertEqual(json.loads(out)["trial_balance"][0]["due"], "2026-10-01T17:00:00Z")

    def test_errors_exit_3(self):
        code, _, err = self.run_cli("owe", "R-1", "a", "s", "--due", "whenever")
        self.assertEqual(code, 3)
        self.assertIn("due is not an ISO 8601 time", err)
        self.run_cli("owe", "R-1", "a", "s")
        code, _, err = self.run_cli("balance", "--as-of", "later")
        self.assertEqual(code, 3)
        with open(self.path, "a") as fh:
            fh.write("garbage\n")
        code, _, err = self.run_cli("verify")
        self.assertEqual(code, 3)
        self.assertIn("line 2", err)
        code, _, err = self.run_cli("report", "--html", os.path.join(self.dir, "no", "such", "dir.html"))
        self.assertEqual(code, 3)

    def test_report_to_unwritable_path_after_good_ledger(self):
        self.run_cli("owe", "R-1", "a", "s")
        code, _, err = self.run_cli("report", "--html", os.path.join(self.dir, "missing", "r.html"))
        self.assertEqual(code, 3)
        self.assertIn("error:", err)

    def test_keyboard_interrupt_in_watch_exits_cleanly(self):
        self.run_cli("owe", "R-1", "a", "s")
        import delegationledger.cli as c
        real = c.watch

        def boom(*a, **k):
            raise KeyboardInterrupt
        c.watch = boom
        try:
            self.assertEqual(self.run_cli("watch")[0], 0)
        finally:
            c.watch = real


class AdapterErrors(unittest.TestCase):
    def test_commission_checks_the_whole_plan_first(self):
        led = Ledger()
        for bad in ([{"step": "a"}, {"amount": 5}], ["a", 7], ["a", {"step": "b", "amount": "ten"}],
                    ["a", {"step": "b", "due": "soon"}], ["a", ""]):
            with self.assertRaises(LedgerError):
                commission(led, "T", "x", bad)
        self.assertEqual(led.entries, [])   # nothing half-written

    def test_observed_propagates_a_failed_record(self):
        led = Ledger()

        @observed(led, "x", amount_arg="amount")
        def pay(*, task_id, amount):
            return "paid"
        with self.assertRaises(LedgerError):
            pay(task_id="T", amount="lots")


class Example(unittest.TestCase):
    def run_example(self, *args):
        return subprocess.run([sys.executable, "-W", "error", os.path.join(ROOT, "examples", "agent_loop.py"), *args],
                              capture_output=True, text=True)

    def test_clean_run_balances(self):
        r = self.run_example()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("skipped 0  amount 0  unowed 0  chain verified", r.stdout)

    def test_skip_and_wrong_amount_are_caught(self):
        r = self.run_example("--skip", "post_goods_receipt")
        self.assertEqual(r.returncode, 1)
        self.assertIn("post_goods_receipt done.", r.stdout)      # the agent claims it
        self.assertIn("skipped 1", r.stdout)                     # the ledger does not believe it
        r = self.run_example("--wrong-amount", "post_invoice")
        self.assertEqual(r.returncode, 1)
        self.assertIn("amount 1", r.stdout)

    def test_keep_writes_a_verifiable_file(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "e.jsonl")
            self.assertEqual(self.run_example("--ledger", path).returncode, 0)
            self.assertTrue(Ledger(path).verify())
            self.assertEqual(len(Ledger(path).entries), 11)   # 5 owe, 5 ran, 1 close
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
