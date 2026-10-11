"""Hostile and extreme input: empty, huge, malformed, Unicode, mixed line
endings, duplicates, out-of-range values, markup in every text field, deep
nesting, negative and NaN numbers, dates across DST and year boundaries."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from delegationledger import (ChainError, Ledger, LedgerError, canonical, entry_hash, format_table, load,  # noqa: E402
                              norm_amount, printable, render, same_amount, verify_chain)
from delegationledger.ledger import FORMAT, GENESIS  # noqa: E402

XSS = ['<script>document.documentElement.dataset.pwned=1</script>',
       '<img src=x onerror="document.documentElement.dataset.pwned=1">']


def forge(bodies):
    """Lines with a valid chain over whatever the bodies hold: what an
    attacker who can compute SHA-256 would write."""
    out, prev = [], GENESIS
    for i, b in enumerate(bodies, 1):
        b = dict(b, seq=i, prev=prev)
        e = dict(b, hash=entry_hash(b))
        out.append(canonical(e))
        prev = e["hash"]
    return "".join(x + "\n" for x in out)


def owe_body(**kw):
    b = {"fmt": FORMAT, "book": "owe", "role": "commissioner", "task_id": "T", "agent": "a", "step": "s",
         "amount": None, "due": None, "by": "x", "ts": "2026-10-01T00:00:00Z"}
    b.update(kw)
    return b


class Files(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "h.jsonl")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write(self, data):
        with open(self.path, "wb") as fh:
            fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))

    def assertChain(self, data, line, fragment):
        self.write(data)
        with self.assertRaises(ChainError) as cm:
            Ledger(self.path)
        self.assertEqual(cm.exception.line, line)
        self.assertIn(fragment, str(cm.exception))

    def test_empty_file(self):
        self.write(b"")
        led = Ledger(self.path)
        self.assertEqual(led.entries, [])
        self.assertTrue(led.balance().balanced)
        self.assertTrue(led.verify())
        led.owe("T", "a", "s")
        self.assertEqual(len(Ledger(self.path).entries), 1)

    def test_malformed_lines(self):
        self.assertChain(b"not json\n", 1, "not JSON")
        self.assertChain(b"5\n", 1, "not a JSON object")
        self.assertChain(b"[1,2]\n", 1, "not a JSON object")
        self.assertChain(b"{}\n", 1, "no hash field")
        self.assertChain(b"\n", 1, "blank line")
        self.assertChain(b"\xff\xfe\n", 1, "not valid UTF-8")
        self.assertChain(("[" * 200000 + "]" * 200000 + "\n").encode(), 1, "")
        self.assertChain(b'{"a":' * 100000 + b"1" + b"}" * 100000 + b"\n", 1, "")

    def test_forged_fields_are_refused_before_hashing(self):
        cases = [
            (owe_body(amount=5.5), "amount is not a plain decimal string"),
            (owe_body(amount="1E+3"), "amount is not a plain decimal string"),
            (owe_body(amount=XSS[1]), "amount is not a plain decimal string"),
            (owe_body(amount="NaN"), "amount is not a plain decimal string"),
            (owe_body(task_id=""), "task_id must be a non-empty string"),
            (owe_body(task_id="\ud800"), "lone surrogate"),
            (owe_body(task_id=["x"]), "task_id must be a non-empty string"),
            (owe_body(due="tomorrow"), "due is not an ISO 8601 time"),
            (owe_body(due=5), "due is not an ISO 8601 time"),
            (owe_body(fmt="other/9"), "unknown format"),
            (owe_body(book="pay"), "unknown book"),
            (owe_body(role="harness"), "must carry role"),
            (dict(owe_body(), extra={"deep": [[[1]]]}), "unexpected extra"),
            (dict(owe_body(book="ran", role="harness"), due="2026-10-01T00:00:00Z"), "carries no due time"),
            (dict(owe_body(book="close"), agent="a"), "carries only a task_id"),
            ({k: v for k, v in owe_body().items() if k != "ts"}, "missing ts"),
        ]
        for body, fragment in cases:
            with self.subTest(fragment=fragment):
                self.write(forge([body]) if body.get("task_id") != "\ud800" else
                           (json.dumps(dict(body, seq=1, prev=GENESIS, hash="0" * 64)) + "\n"))
                with self.assertRaises(ChainError) as cm:
                    Ledger(self.path)
                self.assertIn(fragment, str(cm.exception))

    def test_float_seq_and_bool_seq_are_refused(self):
        b = owe_body()
        for seq in (1.0, True):
            e = dict(b, seq=seq, prev=GENESIS)
            self.write(json.dumps(dict(e, hash=entry_hash(e))) + "\n")
            with self.assertRaises(ChainError) as cm:
                Ledger(self.path)
            self.assertIn("seq is not an integer", str(cm.exception))

    def test_bad_hash_and_prev_formats(self):
        e = dict(owe_body(), seq=1, prev="xyz", hash="0" * 64)
        self.write(json.dumps(e) + "\n")
        with self.assertRaises(ChainError):
            Ledger(self.path)
        e = dict(owe_body(), seq=1, prev=GENESIS, hash="ABC")
        self.write(json.dumps(e) + "\n")
        with self.assertRaises(ChainError):
            Ledger(self.path)

    def test_seal_and_carry_field_checks(self):
        seal = {"fmt": FORMAT, "book": "seal", "role": "commissioner", "by": "x", "ts": "t"}
        for body, frag in ((dict(seal, segment="", lines=1), "segment is not a segment file name"),
                           (dict(seal, segment="a.000001.jsonl", lines=0), "lines is not a positive integer")):
            self.write(forge([body]))
            with self.assertRaises(ChainError) as cm:
                Ledger(self.path)
            self.assertIn(frag, str(cm.exception))
        carry = {"fmt": FORMAT, "book": "carry", "role": "commissioner", "by": "x", "ts": "t", "from": ""}
        with self.assertRaises(ChainError) as cm:
            verify_chain([dict(carry, head=GENESIS, seq=1, prev=GENESIS, hash="0" * 64)])
        self.assertIn("from is not a segment file name", str(cm.exception))
        with self.assertRaises(ChainError) as cm:
            verify_chain([dict(carry, **{"from": "a.000001.jsonl"}, head="1" * 64, seq=1, prev=GENESIS, hash="0" * 64)])
        self.assertIn("head must equal its prev", str(cm.exception))

    def test_carry_in_the_middle_is_refused(self):
        led = Ledger()
        led.owe("T", "a", "s")
        b = {"fmt": FORMAT, "book": "carry", "role": "commissioner", "by": "x", "ts": "t",
             "from": "x.000001.jsonl", "head": led.head, "seq": 2, "prev": led.head}
        with self.assertRaises(ChainError) as cm:
            verify_chain(led.entries + [dict(b, hash=entry_hash(b))])
        self.assertIn("must open a file or follow a seal", str(cm.exception))

    def test_unicode_round_trips(self):
        names = ["R- -sep", "R-\x85-nel", "R- ", "مهمة", "é", "\U0001F9FE",
                 "tab\there", "nl\nhere", "quote\"back\\slash", "\x1cx\x1d", "\u00a0", "\x7f", "‮evil"]
        led = Ledger(self.path)
        for n in names:
            led.owe(n, n, n, amount=1)
            led.ran(n, n, n, amount="1.0")
        again = Ledger(self.path)
        self.assertEqual(again.entries, led.entries)
        self.assertTrue(again.balance().balanced)
        self.assertEqual({e["task_id"] for e in again.entries}, set(names))
        self.assertNotIn("\x1b", format_table(again.trial_balance()))

    def test_lone_surrogate_is_refused_at_write(self):
        with self.assertRaises(LedgerError):
            Ledger().owe("T\udc00", "a", "s")
        with self.assertRaises(LedgerError):
            Ledger().ran("T", "a", "s", by="\ud800")

    def test_mixed_line_endings_and_bom(self):
        led = Ledger(self.path)
        for i in range(4):
            led.owe(f"T{i}", "a", "s")
        with open(self.path, "rb") as fh:
            ls = fh.read().split(b"\n")[:-1]
        self.write(b"\xef\xbb\xbf" + ls[0] + b"\r\n" + ls[1] + b"\n" + ls[2] + b"\r\n" + ls[3] + b"\n")
        again = Ledger(self.path)
        self.assertEqual(len(again.entries), 4)
        again.owe("T9", "a", "s")         # appends after a BOM and CRLF lines
        self.assertEqual(len(Ledger(self.path).entries), 5)
        self.write(ls[0] + b"\r" + ls[1] + b"\r")   # bare CR: one long line, refused
        with self.assertRaises(ChainError):
            Ledger(self.path)

    def test_duplicate_ids_and_duplicate_lines(self):
        led = Ledger(self.path)
        led.owe("T", "a", "s")
        led.owe("T", "a", "s")
        led.ran("T", "a", "s")
        self.assertEqual([(r.owed, r.ran, r.status) for r in led.rows()], [(2, 1, "open")])
        with open(self.path, "rb") as fh:
            ls = fh.read().split(b"\n")[:-1]
        self.write(ls[0] + b"\n" + ls[0] + b"\n")      # the same line twice
        with self.assertRaises(ChainError) as cm:
            Ledger(self.path)
        self.assertEqual(cm.exception.line, 2)

    def test_markup_in_every_text_field_is_escaped_in_the_report(self):
        led = Ledger(self.path, clock=lambda: XSS[0])
        for x in XSS:
            led.owe(x, x, x, by=x)
            led.ran(x, x, x + "2", by=x)
            led.close(x, by=x)
        page = render(led)
        for x in XSS:
            self.assertNotIn(x, page)
        self.assertNotIn("<script>document", page)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;img src=x onerror=&quot;", page)


class Values(unittest.TestCase):
    def test_amount_normalisation(self):
        cases = {"1e3": "1000", "120.50": "120.50", 120.5: "120.5", "-0": "0", "-0.00": "0.00", " 7 ": "7",
                 -3: "-3", "0.000000000000000001": "0.000000000000000001", 10 ** 29: str(10 ** 29),
                 "1.5E+2": "150", 1e-7: "0.0000001"}
        for given, want in cases.items():
            with self.subTest(given=given):
                self.assertEqual(norm_amount(given), want)
        self.assertIsNone(norm_amount(None))

    def test_amount_refusals(self):
        for bad in (float("nan"), float("inf"), "-inf", "NaN", "ten", "", True, "1e30", 10 ** 30,
                    "0.0000000000000000001", "1e999999", "0x10", [1]):
            with self.subTest(bad=bad), self.assertRaises(LedgerError):
                norm_amount(bad)

    def test_same_amount(self):
        self.assertTrue(same_amount("10", "10.000"))
        self.assertTrue(same_amount(None, None))
        self.assertFalse(same_amount("10", None))
        self.assertFalse(same_amount("-10", "10"))

    def test_negative_amounts_balance_and_mismatch(self):
        led = Ledger()
        led.owe("T", "a", "refund", amount=-25)
        led.ran("T", "a", "refund", amount="-25.00")
        led.owe("T", "a", "credit", amount=-5)
        led.ran("T", "a", "credit", amount=5)
        self.assertEqual([m["step"] for m in led.balance().amount_mismatches], ["credit"])

    def test_names_out_of_range(self):
        with self.assertRaises(LedgerError):
            Ledger().owe("T" * 513, "a", "s")
        Ledger().owe("T" * 512, "a", "s")
        for blank in ("   ", "\t\n", ""):
            with self.assertRaises(LedgerError):
                Ledger().owe(blank, "a", "s")
        with self.assertRaises(LedgerError):
            Ledger().owe(5, "a", "s")

    def test_clock_must_return_text(self):
        with self.assertRaises(LedgerError):
            Ledger(clock=lambda: 5).owe("T", "a", "s")

    def test_dst_and_year_boundaries(self):
        led = Ledger()
        # Chicago falls back on 1 Nov 2026: 01:30 CDT (-05:00) comes before 01:10 CST (-06:00)
        led.owe("T", "a", "s1", due="2026-11-01T01:30:00-05:00")
        led.owe("T", "a", "s1", due="2026-11-01T01:10:00-06:00")
        # New Year's Eve in Chicago is already 2027 in UTC
        e = led.owe("T", "a", "s2", due="2026-12-31T23:30:00-06:00")
        self.assertEqual(e["due"], "2027-01-01T05:30:00Z")
        # spring forward: 8 Mar 2026 02:30 does not exist in Chicago; an explicit offset is still exact
        led.owe("T", "a", "s3", due="2026-03-08T03:30:00-05:00")
        row = {r.step: r for r in led.rows("2026-01-01T00:00:00Z")}
        self.assertEqual(row["s1"].due, "2026-11-01T06:30:00Z")   # earliest instant, not earliest string
        self.assertEqual(sorted(s["step"] for s in led.balance("2027-01-01T05:29:59Z").skipped_steps), ["s1", "s3"])
        self.assertEqual(sorted(s["step"] for s in led.balance("2027-01-01T05:30:01Z").skipped_steps),
                         ["s1", "s2", "s3"])
        self.assertEqual(led.owe("T", "a", "s4", due="2026-10-01T09:00:00")["due"], "2026-10-01T09:00:00Z")

    def test_bad_times_raise_ledger_errors(self):
        with self.assertRaises(LedgerError):
            Ledger().owe("T", "a", "s", due="31/12/2026")
        with self.assertRaises(LedgerError):
            Ledger().balance(as_of="soon")
        with self.assertRaises(LedgerError):
            Ledger().rows(as_of="soon")

    def test_printable(self):
        self.assertEqual(printable("a\x1b[31mb"), "a\\x1b[31mb")
        self.assertEqual(printable("x y"), "x\\u2028y")
        self.assertEqual(printable("plain"), "plain")


class Huge(unittest.TestCase):
    def test_100x_sample_appends_and_balances(self):
        """12,200 lines, 100 times the demo sample, through the file."""
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "big.jsonl")
            led = Ledger(path, clock=lambda: "2026-10-01T00:00:00Z")
            t0 = time.perf_counter()
            for i in range(12200 // 2):
                led.owe(f"R-{i // 6:05d}", "buyer", f"step{i % 6}", amount=i)
                led.ran(f"R-{i // 6:05d}", "buyer", f"step{i % 6}", amount=i)
            append_s = time.perf_counter() - t0
            t0 = time.perf_counter()
            again = Ledger(path)
            self.assertTrue(again.balance().balanced)
            load_s = time.perf_counter() - t0
            self.assertEqual(len(again.entries), 12200)
            self.assertLess(append_s, 30)  # was quadratic: 3,000 appends took 20 s before 0.2
            self.assertLess(load_s, 10)
        finally:
            shutil.rmtree(d)

    def test_load_of_a_single_file(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "one.jsonl")
            Ledger(path).owe("T", "a", "s")
            self.assertEqual(len(load(path)), 1)
        finally:
            shutil.rmtree(d)


class SecondCheck(unittest.TestCase):
    """One test per code defect from the second independent check."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "s.jsonl")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_1_owe_and_ran_need_agent_and_step(self):
        led = Ledger(self.path)
        for call in (lambda: led.owe("T", None, "s"), lambda: led.owe("T", "a", None), lambda: led.owe("T", "", "s"),
                     lambda: led.ran("T", None, "s"), lambda: led.ran("T", "a", None), lambda: led.ran("T", "a", " ")):
            with self.assertRaises(LedgerError):
                call()
        for call in (lambda: Ledger.open(self.path, "harness").ran("T", None, "s"),
                     lambda: Ledger.open(self.path, "commissioner").owe("T", "a", None)):
            with self.assertRaises(LedgerError):
                call()
        led.close("T")                        # close alone carries no agent or step
        self.assertEqual(len(Ledger(self.path).entries), 1)   # nothing the loader rejects was written

    def test_5_time_that_overflows_in_utc(self):
        for due in ("0001-01-01T00:30:00+01:00", "9999-12-31T23:30:00-01:00"):
            with self.assertRaises(LedgerError) as cm:
                Ledger().owe("T", "a", "s", due=due)
            self.assertIn("out of range", str(cm.exception))
        with self.assertRaises(LedgerError):
            Ledger().balance(as_of="0001-01-01T00:00:00+05:00")
        from delegationledger.cli import main
        import io as _io
        from contextlib import redirect_stderr
        err = _io.StringIO()
        with redirect_stderr(err):
            code = main(["--ledger", self.path, "owe", "T", "a", "s", "--due", "0001-01-01T00:30:00+01:00"])
        self.assertEqual(code, 3)
        self.assertIn("0001-01-01T00:30:00+01:00", err.getvalue())

    def test_6_failed_write_leaves_no_partial_line(self):
        from unittest import mock
        led = Ledger(self.path)
        led.owe("T", "a", "s")
        size = os.path.getsize(self.path)
        with mock.patch("delegationledger.ledger.os.fsync", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                led.owe("T", "a", "t")
        self.assertEqual(os.path.getsize(self.path), size)
        self.assertEqual(len(led.entries), 1)
        led.owe("T", "a", "t")                # the next append works
        self.assertTrue(Ledger(self.path).verify())

    @unittest.skipUnless(hasattr(os, "fork") and sys.platform != "win32", "needs RLIMIT_FSIZE")
    def test_6_file_size_limit_mid_line(self):
        import subprocess
        code = (
            "import os, resource, signal, sys; sys.path.insert(0, %r)\n"
            "from delegationledger import Ledger\n"
            "led = Ledger(%r); led.owe('T', 'a', 's')\n"
            "size = os.path.getsize(%r)\n"
            "signal.signal(signal.SIGXFSZ, signal.SIG_IGN)\n"
            "resource.setrlimit(resource.RLIMIT_FSIZE, (size + 60, resource.RLIM_INFINITY))\n"
            "try:\n    led.owe('T', 'a', 'x' * 300)\nexcept OSError:\n    print('refused')\n"
            "print(os.path.getsize(%r) == size)\n"
        ) % (os.path.dirname(os.path.dirname(os.path.abspath(__file__))), self.path, self.path, self.path)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(r.stdout.split(), ["refused", "True"], r.stderr)
        self.assertTrue(Ledger(self.path).verify())

    def test_6_failed_carry_write_removes_tmp(self):
        from unittest import mock
        led = Ledger(self.path)
        led.owe("T", "a", "s")
        real = os.replace
        with mock.patch("delegationledger.ledger.os.replace", side_effect=OSError("no")):
            with self.assertRaises(OSError):
                led.rotate()
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        with mock.patch("delegationledger.ledger.os.replace", real):
            Ledger(self.path).owe("T", "a", "t")   # heals: writes the carry, then the line
        self.assertTrue(Ledger(self.path).verify())

    def test_7_seal_segment_must_be_a_bare_segment_name(self):
        seal = {"fmt": FORMAT, "book": "seal", "role": "commissioner", "by": "x", "ts": "t", "lines": 1}
        for bad in ("../s.000001.jsonl", "/tmp/s.000001.jsonl", "sub/s.000001.jsonl", "sub\\s.000001.jsonl",
                    "..000001.jsonl", "s.jsonl", ".hidden.000001.jsonl", "s.000001.jsonl\n"):
            with self.subTest(bad=bad):
                self.write_seal(dict(seal, segment=bad))
                with self.assertRaises(ChainError) as cm:
                    Ledger(self.path)
                self.assertIn("segment is not a segment file name", str(cm.exception))
        os.remove(self.path)
        disk = Ledger(self.path)
        disk.owe("T", "a", "s")
        disk._sync()
        disk._write({"fmt": FORMAT, "book": "seal", "role": "commissioner", "segment": "../escape.000001.jsonl",
                     "lines": 2, "by": "x", "ts": "t"})
        with self.assertRaises(LedgerError):
            disk._finish_rotation()
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.dir), "escape.000001.jsonl")))

    def write_seal(self, body):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(forge([body]))

    def test_13_amount_rules(self):
        led = Ledger()
        led.owe("T", "a", "no_amount_owed")
        led.ran("T", "a", "no_amount_owed", amount=99)       # an obligation with no amount accepts any run amount
        led.owe("T", "a", "amount_owed", amount=10)
        led.ran("T", "a", "amount_owed")                      # an obligation with an amount requires one
        st = {r.step: r.status for r in led.rows()}
        self.assertEqual(st, {"no_amount_owed": "ok", "amount_owed": "amount"})


if __name__ == "__main__":
    unittest.main()
