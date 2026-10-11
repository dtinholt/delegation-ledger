"""The five fault classes of the Trial Balance campaign, one per test."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from delegationledger import Ledger  # noqa: E402
import procurement as P  # noqa: E402

AMOUNT = 5955  # above the approval threshold, so all seven steps are owed


class FaultClasses(unittest.TestCase):
    def setUp(self):
        self.led = Ledger()

    def test_N_no_fault_balances(self):
        P.run_requisition(self.led, "R-0001", "buyer", AMOUNT, "N")
        b = self.led.balance()
        self.assertTrue(b.balanced)
        self.assertEqual(b.imbalances, [])
        self.assertTrue(all(r["status"] == "ok" for r in self.led.trial_balance()))
        self.assertEqual(len(self.led.trial_balance()), 7)

    def test_O1_skipped_step_is_found(self):
        P.run_requisition(self.led, "R-0002", "buyer", AMOUNT, "O1", skip="post_goods_receipt")
        b = self.led.balance()
        self.assertEqual([(s["task_id"], s["step"]) for s in b.skipped_steps],
                         [("R-0002", "post_goods_receipt")])
        self.assertEqual(b.amount_mismatches, [])
        self.assertEqual(b.open_obligations, [])

    def test_O2_false_completion_line_changes_nothing(self):
        diary = P.run_requisition(self.led, "R-0003", "buyer", AMOUNT, "O2", skip="request_approval")
        self.assertIn("R-0003: request_approval done.", diary)  # the agent claims it
        b = self.led.balance()
        self.assertEqual([(s["task_id"], s["step"]) for s in b.skipped_steps],
                         [("R-0003", "request_approval")])
        row = [r for r in self.led.trial_balance() if r["step"] == "request_approval"][0]
        self.assertEqual((row["owed"], row["ran"], row["status"]), (1, 0, "skipped"))

    def test_C1_wrong_amount_is_found(self):
        P.run_requisition(self.led, "R-0004", "buyer", AMOUNT, "C1", wrong_step="create_po")
        b = self.led.balance()
        self.assertEqual(b.skipped_steps, [])
        self.assertEqual(len(b.amount_mismatches), 1)
        m = b.amount_mismatches[0]
        self.assertEqual((m["task_id"], m["step"]), ("R-0004", "create_po"))
        self.assertEqual(m["owed_amounts"], ["5955"])
        self.assertEqual(m["ran_amounts"], ["9555"])

    def test_C2_wrong_amount_on_both_sides_is_a_blind_spot(self):
        diary = P.run_requisition(self.led, "R-0005", "buyer", AMOUNT, "C2")
        b = self.led.balance()
        # by construction the books agree, so the balance reports nothing
        self.assertTrue(b.balanced)
        self.assertEqual(b.imbalances, [])
        # ... although every posted amount is wrong against the vendor quote
        posted = {e["step"]: e["amount"] for e in self.led.entries if e["book"] == "ran" and e["amount"]}
        self.assertEqual(set(posted.values()), {"9555"})
        self.assertTrue(any("quote 5955" in line for line in diary))
        # and the balance says so in words, every time
        self.assertIn("both sides", b.blind_spot)
        self.assertIn("0 of 1,044", b.blind_spot)


class AllFiveInOneLedger(unittest.TestCase):
    def test_mixed_book_reports_exactly_the_three_visible_faults(self):
        led = Ledger()
        for i, c in enumerate(P.CLASSES, 1):
            P.run_requisition(led, f"R-{i:04d}", "buyer", AMOUNT, c)
        b = led.balance()
        self.assertEqual(sorted(s["task_id"] for s in b.skipped_steps), ["R-0002", "R-0003"])
        self.assertEqual([m["task_id"] for m in b.amount_mismatches], ["R-0004"])
        self.assertNotIn("R-0005", {x["task_id"] for x in b.imbalances})


if __name__ == "__main__":
    unittest.main()
