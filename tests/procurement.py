"""The campaign's procurement workflow, rebuilt for tests and the demo.

Seven steps per requisition, approval only above 5,000. The commissioning layer
knows the requisition amount and writes obligations from it. The harness runs
the steps and records what it ran. Five fault conditions, as in the Trial
Balance campaign:

  N   no fault
  O1  a skipped step
  O2  a skipped step, and the agent's diary claims it was done
  C1  one amount-bearing step posts a wrong amount
  C2  the requisition amount was wrong before assignment, so both books carry it
"""
STEPS = ["check_vendor", "get_quote", "create_po", "request_approval",
         "post_goods_receipt", "post_invoice", "schedule_payment"]
AMOUNT_STEPS = {"create_po", "post_invoice", "schedule_payment"}
APPROVAL_THRESHOLD = 5000
CLASSES = ["N", "O1", "O2", "C1", "C2"]


def obligations(amount):
    out = []
    for s in STEPS:
        if s == "request_approval" and amount <= APPROVAL_THRESHOLD:
            continue
        out.append((s, amount if s in AMOUNT_STEPS else None))
    return out


def transpose(amount):
    """5955 -> 9555; x10 when no transposition changes the number (campaign rule)."""
    d = str(amount)
    for i in range(len(d) - 1):
        if d[i] != d[i + 1]:
            t = d[:i] + d[i + 1] + d[i] + d[i + 2:]
            if t[0] != "0":
                return int(t)
    return amount * 10


def run_requisition(ledger, task_id, agent, true_amount, fault="N",
                    skip="post_goods_receipt", wrong_step="create_po", diary=None):
    """Commission and execute one requisition with one fault. Returns the
    agent's diary (a list of strings the agent wrote; it never reaches the
    ledger)."""
    diary = [] if diary is None else diary
    assigned = transpose(true_amount) if fault == "C2" else true_amount
    for step, amt in obligations(assigned):           # commissioning layer, before the agent acts
        ledger.owe(task_id, agent, step, amount=amt)
    quote = true_amount                                # the vendor's quote is the source of truth
    for step, amt in obligations(assigned):           # the agent works; the harness records
        if fault in ("O1", "O2") and step == skip:
            if fault == "O2":
                diary.append(f"{task_id}: {step} done.")
            continue
        posted = amt
        if fault == "C1" and step == wrong_step:
            posted = transpose(amt)
        diary.append(f"{task_id}: {step}" + (f" {posted}" if posted is not None else "")
                     + (f" (quote {quote})" if step == "get_quote" else ""))
        ledger.ran(task_id, agent, step, amount=posted)
    return diary
