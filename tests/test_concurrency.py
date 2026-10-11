"""Several processes appending to one ledger at once, with and without a
rotation running alongside them. The chain must never fork or interleave."""
import json
import multiprocessing as mp
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from delegationledger import Ledger, verify_series  # noqa: E402
from delegationledger.ledger import fcntl  # noqa: E402
import _workers  # noqa: E402

WRITERS, LINES = 4, 120


@unittest.skipIf(fcntl is None, "appends are unlocked on this platform")
class TwoProcessesAtOnce(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "shared.jsonl")
        self.ctx = mp.get_context("spawn")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def _run(self, targets):
        start = self.ctx.Event()
        procs = [self.ctx.Process(target=t, args=a + (start,)) for t, a in targets]
        for p in procs:
            p.start()
        start.set()
        for p in procs:
            p.join(120)
            self.assertEqual(p.exitcode, 0)

    def _assert_whole_lines(self, path):
        with open(path, "rb") as fh:
            data = fh.read()
        self.assertTrue(data.endswith(b"\n"))
        for line in data.split(b"\n")[:-1]:
            json.loads(line)  # no interleaved bytes inside a line

    def test_two_processes_never_fork_the_chain(self):
        self._run([(_workers.appender, (self.path, w, LINES)) for w in range(2)])
        led = Ledger(self.path)
        self.assertTrue(led.verify())
        self.assertEqual(len(led.entries), 2 * LINES)
        self.assertEqual([e["seq"] for e in led.entries], list(range(1, 2 * LINES + 1)))
        self._assert_whole_lines(self.path)
        by = [e["by"] for e in led.entries]
        self.assertGreater(sum(1 for x, y in zip(by, by[1:]) if x != y), 1)  # the writers really interleaved
        # each writer's own lines stay in its own order
        for w in range(2):
            mine = [e for e in led.entries if e["by"] == f"writer{w}"]
            self.assertEqual(len(mine), LINES)
            self.assertEqual([e["amount"] for e in mine if e["book"] == "owe"],
                             [str(i) for i in range(0, LINES, 2)])

    def test_four_writers_balance_exactly(self):
        self._run([(_workers.appender, (self.path, w, LINES)) for w in range(WRITERS)])
        led = Ledger(self.path)
        self.assertEqual(len(led.entries), WRITERS * LINES)
        b = led.balance()
        self.assertTrue(b.balanced, b.to_dict())  # every owe got its ran at the same amount

    def test_appends_survive_a_concurrent_rotation(self):
        self._run([(_workers.appender, (self.path, w, LINES)) for w in range(3)]
                  + [(_workers.rotator, (self.path, 5))])
        s = verify_series(self.path)
        led = Ledger(self.path)
        work = [e for e in led.entries if e["book"] in ("owe", "ran")]
        self.assertEqual(len(work), 3 * LINES)
        self.assertEqual(s["entries"], len(led.entries))
        self.assertGreaterEqual(len(led.segments), 1)
        self.assertTrue(led.balance().balanced)
        for seg in led.segments:
            self._assert_whole_lines(os.path.join(self.dir, seg))


if __name__ == "__main__":
    unittest.main()
