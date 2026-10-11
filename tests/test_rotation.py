"""rotate(): the seal line, the carry line, verify() across a rotated series,
tampering with a series, and recovery from a crash part way through."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from delegationledger import (ChainError, Ledger, LedgerError, RoleError, load, segment_name,  # noqa: E402
                              segment_paths, verify_series)


def lines(path):
    with open(path) as fh:
        return [json.loads(x) for x in fh.read().splitlines()]


class Rotation(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "ledger.jsonl")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def _fill(self, led, task, n=3):
        for i in range(n):
            led.owe(task, "a", f"s{i}", amount=10 * i)
        for i in range(n):
            led.ran(task, "a", f"s{i}", amount=10 * i)

    def test_segment_names(self):
        self.assertEqual(segment_name(self.path, 1), "ledger.000001.jsonl")
        self.assertEqual(segment_name(os.path.join(self.dir, "plain"), 12), "plain.000012")
        self.assertEqual(segment_paths(os.path.join(self.dir, "missing", "x.jsonl")), [])

    def test_rotate_seals_and_carries_the_head(self):
        led = Ledger(self.path)
        self._fill(led, "T1")
        before = led.head
        r = led.rotate()
        self.assertEqual(r["segment"], "ledger.000001.jsonl")
        old = lines(os.path.join(self.dir, r["segment"]))
        seal = old[-1]
        self.assertEqual(seal["book"], "seal")
        self.assertEqual(seal["prev"], before)
        self.assertEqual(seal["segment"], "ledger.000001.jsonl")
        self.assertEqual(seal["lines"], len(old))
        self.assertEqual(r["head"], seal["hash"])
        new = lines(self.path)
        self.assertEqual(len(new), 1)
        carry = new[0]
        self.assertEqual((carry["book"], carry["head"], carry["prev"], carry["from"]),
                         ("carry", seal["hash"], seal["hash"], "ledger.000001.jsonl"))
        self.assertEqual(carry["seq"], seal["seq"] + 1)
        self.assertEqual(led.segments, ["ledger.000001.jsonl"])

    def test_verify_and_balance_across_a_rotated_series(self):
        led = Ledger(self.path)
        led.owe("T1", "a", "early", amount=5)       # owed in segment 1 ...
        self._fill(led, "T0")
        led.rotate()
        self._fill(led, "T2")
        led.rotate()
        led.ran("T1", "a", "early", amount=6)       # ... ran in the live file
        again = Ledger(self.path)
        self.assertTrue(again.verify())
        self.assertEqual(again.segments, ["ledger.000001.jsonl", "ledger.000002.jsonl"])
        self.assertEqual(len(again.entries), len(led.entries))
        b = again.balance()
        self.assertEqual([(m["task_id"], m["owed_amounts"], m["ran_amounts"]) for m in b.amount_mismatches],
                         [("T1", ["5"], ["6"])])
        s = verify_series(self.path)
        self.assertEqual([x["file"] for x in s["segments"]],
                         ["ledger.000001.jsonl", "ledger.000002.jsonl", "ledger.jsonl"])
        self.assertEqual(s["head"], again.head)
        self.assertEqual(s["segments"][1]["first_seq"], s["segments"][0]["last_seq"] + 1)

    def test_each_segment_verifies_on_its_own(self):
        led = Ledger(self.path)
        self._fill(led, "T1")
        led.rotate()
        self._fill(led, "T2")
        led.rotate()
        self.assertEqual(len(load(os.path.join(self.dir, "ledger.000002.jsonl"))), 8)  # carry, 6, seal

    def test_rotation_is_invisible_to_the_balance(self):
        a = Ledger(os.path.join(self.dir, "a.jsonl"), clock=lambda: "2026-10-01T00:00:00Z")
        b = Ledger(os.path.join(self.dir, "b.jsonl"), clock=lambda: "2026-10-01T00:00:00Z")
        for led, rotate in ((a, False), (b, True)):
            led.owe("T", "x", "one", amount=1)
            if rotate:
                led.rotate()
            led.owe("T", "x", "two")
            if rotate:
                led.rotate()
            led.ran("T", "x", "two")
            led.ran("T", "x", "stray")
        self.assertEqual(a.trial_balance("2026-10-02T00:00:00Z"), b.trial_balance("2026-10-02T00:00:00Z"))

    def test_deleting_a_middle_segment_is_detected(self):
        led = Ledger(self.path)
        for t in ("T1", "T2", "T3"):
            self._fill(led, t)
            led.rotate()
        os.remove(os.path.join(self.dir, "ledger.000002.jsonl"))
        with self.assertRaises(ChainError) as cm:
            Ledger(self.path)
        self.assertEqual(cm.exception.file, "ledger.000003.jsonl")
        self.assertFalse(led.verify())

    def test_deleting_the_oldest_segment_is_detected(self):
        led = Ledger(self.path)
        self._fill(led, "T1")
        led.rotate()
        os.remove(os.path.join(self.dir, "ledger.000001.jsonl"))
        with self.assertRaises(ChainError) as cm:
            verify_series(self.path)
        self.assertIn("prev does not match", str(cm.exception))

    def test_editing_a_sealed_segment_is_detected(self):
        led = Ledger(self.path)
        self._fill(led, "T1")
        led.rotate()
        seg = os.path.join(self.dir, "ledger.000001.jsonl")
        es = lines(seg)
        es[2]["amount"] = "999"
        with open(seg, "w") as fh:
            fh.write("".join(json.dumps(e) + "\n" for e in es))
        self.assertFalse(led.verify())
        with self.assertRaises(ChainError) as cm:
            Ledger(self.path)
        self.assertEqual((cm.exception.file, cm.exception.line), ("ledger.000001.jsonl", 3))

    def test_renamed_segment_is_detected(self):
        led = Ledger(self.path)
        self._fill(led, "T1")
        led.rotate()
        os.rename(os.path.join(self.dir, "ledger.000001.jsonl"), os.path.join(self.dir, "ledger.000007.jsonl"))
        with self.assertRaises(ChainError) as cm:
            Ledger(self.path)
        self.assertIn("seal line naming it", str(cm.exception))

    def test_lines_after_a_seal_are_refused(self):
        led = Ledger(self.path)
        self._fill(led, "T1")
        led.rotate()
        seg = os.path.join(self.dir, "ledger.000001.jsonl")
        with open(self.path) as fh:
            live = fh.read()
        with open(seg, "a") as fh:      # someone appends to the sealed file
            fh.write(live)
        with self.assertRaises(ChainError) as cm:
            verify_series(self.path)
        self.assertEqual(cm.exception.file, "ledger.000001.jsonl")
        es = lines(os.path.join(self.dir, "ledger.000001.jsonl"))
        es[-1]["book"] = "owe"          # and a plain line after the seal
        from delegationledger import verify_chain
        with self.assertRaises(ChainError) as cm:
            verify_chain(es[:-1] + [dict(es[0], seq=len(es), prev=es[-2]["hash"])])
        self.assertIn("after a seal", str(cm.exception))

    def test_crash_after_the_seal_is_finished_by_the_next_append(self):
        led = Ledger(self.path)
        self._fill(led, "T1")
        # simulate a crash right after the seal line was written
        led._sync()
        led._write({"fmt": "delegationledger/1", "book": "seal", "role": "commissioner",
                    "segment": "ledger.000001.jsonl", "lines": 7, "by": "commissioning", "ts": "t"})
        other = Ledger(self.path)          # a reader sees a sealed live file and accepts it
        self.assertEqual(other.entries[-1]["book"], "seal")
        other.owe("T2", "a", "s")          # the next writer finishes the rotation first
        self.assertTrue(other.verify())
        self.assertEqual([e["book"] for e in lines(self.path)], ["carry", "owe"])
        self.assertEqual(Ledger(self.path).segments, ["ledger.000001.jsonl"])

    def test_crash_after_the_rename_is_finished_by_the_next_append(self):
        led = Ledger(self.path)
        self._fill(led, "T1")
        led.rotate()
        os.remove(self.path)               # as if the carry line was never written
        led2 = Ledger(self.path)
        self.assertEqual(led2.entries[-1]["book"], "seal")
        led2.ran("T1", "a", "late")
        self.assertEqual([e["book"] for e in lines(self.path)], ["carry", "ran"])
        self.assertTrue(Ledger(self.path).verify())

    def test_refusals(self):
        with self.assertRaises(LedgerError):
            Ledger().rotate()
        led = Ledger(self.path)
        with self.assertRaises(LedgerError):
            led.rotate()                   # nothing written yet
        self._fill(led, "T1")
        with self.assertRaises(LedgerError):
            led.rotate(by="")
        open(os.path.join(self.dir, "ledger.000001.jsonl"), "w").close()  # a stray file in the way
        with self.assertRaises(ChainError):
            led.rotate()                   # an empty archived segment breaks the series

    def test_existing_target_is_not_overwritten(self):
        led = Ledger(self.path)
        self._fill(led, "T1")
        led._sync()
        led._write({"fmt": "delegationledger/1", "book": "seal", "role": "commissioner",
                    "segment": "ledger.000009.jsonl", "lines": 7, "by": "commissioning", "ts": "t"})
        with open(os.path.join(self.dir, "ledger.000009.jsonl"), "w") as fh:
            fh.write("keep me\n")
        with self.assertRaises(LedgerError):
            led.owe("T2", "a", "s")
        with open(os.path.join(self.dir, "ledger.000009.jsonl")) as fh:
            self.assertEqual(fh.read(), "keep me\n")

    def test_role_handles(self):
        com = Ledger.open(self.path, "commissioner")
        har = Ledger.open(self.path, "harness")
        com.owe("T", "x", "a")
        with self.assertRaises(RoleError):
            har.rotate()
        self.assertFalse(hasattr(har, "rotate"))
        com.rotate()
        har.ran("T", "x", "a")
        self.assertEqual(har.segments, ["ledger.000001.jsonl"])
        self.assertTrue(har.balance().balanced)

    @unittest.skipUnless(hasattr(os, "symlink"), "no symlinks")
    def test_rotating_through_a_symlink(self):
        real = os.path.join(self.dir, "real.jsonl")
        Ledger(real).owe("T", "a", "s")
        os.symlink(real, self.path)
        led = Ledger(self.path)
        led.rotate()
        seg = os.path.join(self.dir, "ledger.000001.jsonl")
        self.assertTrue(os.path.islink(seg))
        self.assertEqual(os.path.realpath(seg), os.path.realpath(real))
        self.assertFalse(os.path.islink(self.path))
        self.assertTrue(Ledger(self.path).verify())

    def test_reader_follows_a_rotation_by_another_writer(self):
        reader = Ledger(self.path)
        writer = Ledger(self.path)
        self._fill(writer, "T1")
        self.assertEqual(len(reader.entries), 0)
        reader._sync()
        self.assertEqual(len(reader.entries), 6)
        writer.rotate()
        writer.owe("T2", "a", "s")
        reader._sync()
        self.assertEqual(reader.entries, Ledger(self.path).entries)
        self.assertEqual(reader.segments, ["ledger.000001.jsonl"])


if __name__ == "__main__":
    unittest.main()
