"""Persistence, the hash chain, open versus skipped, unowed runs, the CLI,
the HTML report and the adapter hooks."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from delegationledger import Ledger, ChainError, LedgerError, RoleError, commission, observed, format_table  # noqa: E402
from delegationledger.cli import main as cli  # noqa: E402


def read_lines(path):
    with open(path) as fh:
        return fh.read().splitlines()


def write_lines(path, lines):
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")


def ticking_clock(start=0):
    t = [start]

    def clock():
        t[0] += 1
        return f"2026-10-01T12:{t[0] // 60:02d}:{t[0] % 60:02d}Z"
    return clock


class Chain(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "l.jsonl")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def _write_some(self):
        led = Ledger(self.path, clock=ticking_clock())
        led.owe("T1", "a", "fetch")
        led.owe("T1", "a", "pay", amount=120.5)
        led.ran("T1", "a", "fetch")
        return led

    def test_round_trip_and_verify(self):
        led = self._write_some()
        again = Ledger(self.path)
        self.assertEqual(again.entries, led.entries)
        self.assertTrue(again.verify())
        self.assertEqual([e["seq"] for e in again.entries], [1, 2, 3])
        self.assertEqual(again.entries[1]["amount"], "120.5")

    def test_append_continues_chain_across_instances(self):
        self._write_some()
        Ledger(self.path).ran("T1", "a", "pay", amount="120.50")
        led = Ledger(self.path)
        self.assertEqual(len(led.entries), 4)
        self.assertTrue(led.balance().balanced)  # 120.5 == 120.50

    def test_edit_is_detected(self):
        self._write_some()
        lines = read_lines(self.path)
        e = json.loads(lines[1]); e["amount"] = "12.05"
        lines[1] = json.dumps(e)
        write_lines(self.path, lines)
        with self.assertRaises(ChainError) as cm:
            Ledger(self.path)
        self.assertEqual(cm.exception.line, 2)

    def test_deletion_is_detected(self):
        self._write_some()
        lines = read_lines(self.path)
        write_lines(self.path, [lines[0], lines[2]])
        with self.assertRaises(ChainError) as cm:
            Ledger(self.path)
        self.assertEqual(cm.exception.line, 2)

    def test_truncated_tail_still_verifies(self):
        # dropping the last line cannot be seen from inside the file; anchor the head elsewhere
        led = self._write_some()
        lines = read_lines(self.path)
        write_lines(self.path, lines[:2])
        self.assertNotEqual(Ledger(self.path).head, led.head)

    def test_verify_rereads_the_file(self):
        led = self._write_some()
        self.assertTrue(led.verify())
        lines = read_lines(self.path)
        e = json.loads(lines[0]); e["step"] = "fetch_all"
        lines[0] = json.dumps(e)
        write_lines(self.path, lines)
        self.assertFalse(led.verify())  # the live object sees the edit on disk

    def test_partial_last_line_is_refused_loudly(self):
        led = self._write_some()
        with open(self.path, "a") as fh:
            fh.write('{"seq":4,"book":"ran"')  # an interrupted write
        with self.assertRaises(ChainError) as cm:
            led.ran("T1", "a", "pay", amount=120.5)
        self.assertEqual(cm.exception.line, 4)
        self.assertIn("truncate the partial tail by hand", str(cm.exception))
        with self.assertRaises(ChainError):
            Ledger(self.path)
        self.assertFalse(led.verify())
        with open(self.path) as fh:
            self.assertTrue(fh.read().endswith('"book":"ran"'))  # nothing appended after it

    def test_bad_input_refused(self):
        led = Ledger()
        for bad in (lambda: led.owe("", "a", "s"), lambda: led.owe("t", "a", "s", amount="ten"),
                    lambda: led.ran("t", "a", "s", amount=float("nan")), lambda: led.owe("t", "a", "s", amount=True)):
            with self.assertRaises(LedgerError):
                bad()
        self.assertEqual(led.entries, [])


class Semantics(unittest.TestCase):
    def test_open_until_the_agent_moves_on(self):
        led = Ledger()
        for s in ("a", "b", "c"):
            led.owe("T", "x", s)
        led.ran("T", "x", "a")
        b = led.balance()
        self.assertEqual([o["step"] for o in b.open_obligations], ["b", "c"])
        self.assertEqual(b.skipped_steps, [])
        led.ran("T", "x", "c")  # moved past b
        b = led.balance()
        self.assertEqual([s["step"] for s in b.skipped_steps], ["b"])
        self.assertEqual(b.open_obligations, [])

    def test_overdue_counts_as_skipped(self):
        led = Ledger()
        led.owe("T", "x", "file_report", due="2026-10-01T09:00:00Z")
        self.assertEqual(len(led.balance(as_of="2026-10-01T08:00:00Z").open_obligations), 1)
        self.assertEqual(len(led.balance(as_of="2026-10-01T10:00:00Z").skipped_steps), 1)

    def test_unowed_run(self):
        led = Ledger()
        led.owe("T", "x", "read")
        led.ran("T", "x", "read")
        led.ran("T", "x", "delete_table")
        led.ran("T", "x", "read")
        b = led.balance()
        self.assertEqual(sorted(u["step"] for u in b.unowed_runs), ["delete_table", "read"])

    def test_agents_are_separate_accounts(self):
        led = Ledger()
        led.owe("T", "planner", "plan")
        led.ran("T", "executor", "plan")
        b = led.balance()
        self.assertEqual(len(b.unowed_runs), 1)
        self.assertEqual(len(b.open_obligations), 1)

    def test_ran_without_amount_when_one_was_owed(self):
        led = Ledger()
        led.owe("T", "x", "pay", amount=10)
        led.ran("T", "x", "pay")
        self.assertEqual(len(led.balance().amount_mismatches), 1)

    def test_table_text(self):
        led = Ledger()
        led.owe("T", "x", "pay", amount=10)
        led.ran("T", "x", "pay", amount=11)
        txt = format_table(led.trial_balance())
        self.assertIn("owed amount", txt)
        self.assertIn("amount", txt.splitlines()[-1])


class Roles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "r.jsonl")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_each_handle_writes_only_its_own_book(self):
        com = Ledger.open(self.path, "commissioner")
        har = Ledger.open(self.path, "harness")
        com.owe("T", "x", "a")
        har.ran("T", "x", "a")
        with self.assertRaises(RoleError):
            har.owe("T", "x", "b")
        with self.assertRaises(RoleError):
            har.close("T")
        with self.assertRaises(RoleError):
            com.ran("T", "x", "a")
        with self.assertRaises(RoleError):
            Ledger.open(self.path, "agent")
        roles = [e["role"] for e in Ledger(self.path).entries]
        self.assertEqual(roles, ["commissioner", "harness"])
        self.assertTrue(com.balance().balanced)  # both handles read the same file

    def test_hasattr_is_false_for_the_other_verb(self):
        har = Ledger.open(self.path, "harness")
        com = Ledger.open(self.path, "commissioner")
        self.assertFalse(hasattr(har, "owe"))
        self.assertFalse(hasattr(har, "close"))
        self.assertFalse(hasattr(com, "ran"))
        self.assertTrue(hasattr(har, "ran"))
        with self.assertRaises(AttributeError) as cm:
            har.owe
        self.assertIn("harness handle cannot owe", str(cm.exception))

    def test_role_is_covered_by_the_chain(self):
        Ledger.open(self.path, "commissioner").owe("T", "x", "a")
        Ledger.open(self.path, "harness").ran("T", "x", "a")
        lines = read_lines(self.path)
        e = json.loads(lines[1]); e["role"] = "commissioner"
        write_lines(self.path, [lines[0], json.dumps(e)])
        with self.assertRaises(ChainError) as cm:
            Ledger(self.path)
        self.assertEqual(cm.exception.line, 2)

    def test_close_turns_owed_steps_into_skipped(self):
        com = Ledger.open(self.path, "commissioner")
        har = Ledger.open(self.path, "harness")
        for s in ("a", "b", "c"):
            com.owe("T", "x", s)
        com.owe("U", "x", "z")
        har.ran("T", "x", "a")
        self.assertEqual(len(com.balance().open_obligations), 3)  # no close, no due: open by design
        com.close("T")
        b = har.balance()
        self.assertEqual([(s["step"], s["reason"]) for s in b.skipped_steps], [("b", "closed"), ("c", "closed")])
        self.assertEqual([o["task_id"] for o in b.open_obligations], ["U"])
        com.owe("T", "x", "d")  # owed after the close: open again
        self.assertEqual(sorted(o["step"] for o in com.balance().open_obligations), ["d", "z"])


class Adapters(unittest.TestCase):
    def test_commission_and_observed(self):
        led = Ledger()
        commission(led, "T9", "worker", ["lookup", {"step": "charge", "amount": 40}])

        @observed(led, "worker", amount_arg="amount")
        def charge(*, task_id, amount):
            return "ok"

        @observed(led, "worker", step="lookup")
        def failing(*, task_id):
            raise RuntimeError("tool down")

        self.assertEqual(charge(task_id="T9", amount=40), "ok")
        with self.assertRaises(RuntimeError):
            failing(task_id="T9")
        b = led.balance()
        # lookup was owed before charge, and charge ran: the agent moved past it
        self.assertEqual([o["step"] for o in b.skipped_steps], ["lookup"])
        self.assertEqual(b.amount_mismatches, [])
        with self.assertRaises(TypeError):
            charge("T9", amount=40)


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

    def test_owe_ran_balance_report_verify(self):
        self.assertEqual(self.run_cli("owe", "R-1", "buyer", "create_po", "--amount", "5955")[0], 0)
        self.assertEqual(self.run_cli("ran", "R-1", "buyer", "create_po", "--amount", "9555")[0], 0)
        code, out, _ = self.run_cli("balance")
        self.assertEqual(code, 1)
        self.assertIn("amount 1", out)
        self.assertIn("blind spot", out)
        code, out, _ = self.run_cli("balance", "--json")
        self.assertEqual(json.loads(out)["balance"]["amount_mismatches"][0]["ran_amounts"], ["9555"])
        html_path = os.path.join(self.dir, "r.html")
        self.assertEqual(self.run_cli("report", "--html", html_path)[0], 0)
        page = "\n".join(read_lines(html_path))
        self.assertIn("0 of 1,044", page)
        self.assertIn("create_po", page)
        self.assertEqual(self.run_cli("verify")[0], 0)
        self.assertEqual(self.run_cli("owe", "R-2", "buyer", "post_goods_receipt")[0], 0)
        self.assertEqual(self.run_cli("close", "R-2")[0], 0)
        code, out, _ = self.run_cli("balance", "--json")
        self.assertEqual(json.loads(out)["balance"]["skipped_steps"][0]["reason"], "closed")

    def test_missing_ledger(self):
        self.assertEqual(self.run_cli("balance")[0], 2)

    def test_module_entry_point(self):
        r = subprocess.run([sys.executable, "-m", "delegationledger", "--ledger", self.path, "owe", "T", "a", "s"],
                           cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class Gaps(unittest.TestCase):
    """Paths the other suites leave untouched."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "g.jsonl")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_legacy_refresh_and_handle_properties(self):
        led = Ledger(self.path)
        har = Ledger.open(self.path, "harness")
        led.owe("T", "a", "s")
        led2 = Ledger(self.path)
        led.ran("T", "a", "s")
        led2._refresh()
        self.assertEqual(len(led2.entries), 2)
        self.assertEqual(len(har.entries), 2)
        self.assertEqual(har.head, led.head)
        self.assertEqual(har.path, self.path)
        self.assertTrue(har.verify())
        self.assertEqual(har.trial_balance()[0]["status"], "ok")

    def test_live_file_deleted_between_reads(self):
        led = Ledger(self.path)
        led.owe("T", "a", "s")
        os.remove(self.path)
        led._sync()
        self.assertEqual(led.entries, [])

    def test_due_as_datetime(self):
        from datetime import datetime, timedelta, timezone
        e = Ledger().owe("T", "a", "s", due=datetime(2026, 10, 1, 9, 0, tzinfo=timezone(timedelta(hours=-5))))
        self.assertEqual(e["due"], "2026-10-01T14:00:00Z")
        e = Ledger().owe("T", "a", "s", due=datetime(2026, 10, 1, 9, 0))
        self.assertEqual(e["due"], "2026-10-01T09:00:00Z")

    def test_lock_file_that_cannot_be_opened(self):
        from delegationledger.ledger import fcntl
        if fcntl is None:
            self.skipTest("no locks on this platform")
        Ledger(self.path).owe("T", "a", "s")
        os.remove(self.path + ".lock")
        os.mkdir(self.path + ".lock")          # the lock path is unusable
        self.assertEqual(len(Ledger(self.path).entries), 1)   # a reader still reads
        with self.assertRaises(OSError):
            Ledger(self.path).owe("T", "a", "t")              # a writer refuses to write unlocked

    def test_watch_reports_skipped_steps(self):
        from delegationledger import watch
        led = Ledger(self.path)
        led.owe("T", "a", "s1")
        led.owe("T", "a", "s2")
        led.ran("T", "a", "s2")
        out = io.StringIO()
        watch(self.path, iterations=1, out=out, as_of="2026-10-01T00:00:00Z", now=lambda: "NOW")
        self.assertIn("skipped", out.getvalue())
        self.assertIn("a later owed step ran", out.getvalue())


if __name__ == "__main__":
    unittest.main()
