#!/usr/bin/env python3
"""A generic agent loop wired to the two ledger handles, runnable without a model.

Three parts, as in most agent frameworks:

  dispatch()        hands a task to an agent. It holds the commissioner handle
                    and writes what the agent owes before the agent starts.
  ToolExecutor      runs the tool calls the agent asks for. It holds the harness
                    handle and records each call after the tool returns.
  scripted_policy   stands in for the model. It returns one tool call per turn
                    from a fixed script, and can be told to skip a step or to
                    post a different amount, so you can watch the balance catch
                    it. Swap it for your model call; nothing else changes.

The agent sees only the policy's inputs and the tool results. It never holds a
ledger handle, so it cannot write either book.

    python3 examples/agent_loop.py                       # clean run, balances
    python3 examples/agent_loop.py --skip post_goods_receipt
    python3 examples/agent_loop.py --wrong-amount post_invoice
    python3 examples/agent_loop.py --ledger /tmp/demo.jsonl --keep

Exit code: 0 when the books balance, 1 when anything is skipped, mismatched or
unowed. The amounts are made up for the example.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from delegationledger import Ledger, commission, format_table  # noqa: E402

PLAN = [  # what the dispatcher knows when it assigns the work
    {"step": "check_vendor"},
    {"step": "get_quote"},
    {"step": "create_po", "amount": "4380"},
    {"step": "post_goods_receipt"},
    {"step": "post_invoice", "amount": "4380"},
]


# -- tools: plain functions; they know nothing about the ledger ------------------
def check_vendor(vendor):
    return {"vendor": vendor, "status": "active"}


def get_quote(vendor):
    return {"vendor": vendor, "quote": "4380"}


def create_po(vendor, amount):
    return {"po": "PO-1", "amount": amount}


def post_goods_receipt(po):
    return {"po": po, "received": True}


def post_invoice(po, amount):
    return {"po": po, "invoiced": amount}


TOOLS = {f.__name__: f for f in (check_vendor, get_quote, create_po, post_goods_receipt, post_invoice)}
AMOUNT_ARG = "amount"


class ToolExecutor:
    """Where tool calls run. Holds the harness handle; records after success."""

    def __init__(self, harness):
        self._harness = harness

    def __call__(self, task_id, agent, call):
        name, args = call["tool"], call["args"]
        result = TOOLS[name](**args)                      # raises: nothing recorded
        self._harness.ran(task_id, agent, name, amount=args.get(AMOUNT_ARG))
        return result


def scripted_policy(skip=None, wrong_amount=None):
    """Stands in for a model: returns the next tool call, or None when done."""
    script = [
        {"tool": "check_vendor", "args": {"vendor": "V-7"}},
        {"tool": "get_quote", "args": {"vendor": "V-7"}},
        {"tool": "create_po", "args": {"vendor": "V-7", "amount": "4380"}},
        {"tool": "post_goods_receipt", "args": {"po": "PO-1"}},
        {"tool": "post_invoice", "args": {"po": "PO-1", "amount": "4380"}},
    ]
    turn = {"i": 0}

    def next_call(history):
        while turn["i"] < len(script):
            call = dict(script[turn["i"]], args=dict(script[turn["i"]]["args"]))
            turn["i"] += 1
            if call["tool"] == skip:
                history.append(f"{call['tool']} done.")      # the agent's claim; the ledger never sees it
                continue
            if call["tool"] == wrong_amount:
                call["args"]["amount"] = "3480"
            return call
        return None
    return next_call


def run_agent(task_id, agent, policy, execute, max_turns=20):
    """The loop: ask the policy for a call, execute it, feed the result back."""
    history = []
    for _ in range(max_turns):
        call = policy(history)
        if call is None:
            break
        result = execute(task_id, agent, call)
        history.append(f"{call['tool']} -> {result}")
    return history


def dispatch(commissioner, task_id, agent):
    """Hands out the work: writes the obligations first, from the plan."""
    commission(commissioner, task_id, agent, PLAN)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--ledger", help="ledger file (default: a temporary file)")
    p.add_argument("--keep", action="store_true", help="keep the temporary ledger file")
    p.add_argument("--skip", choices=sorted(TOOLS), help="have the agent skip this step and claim it")
    p.add_argument("--wrong-amount", choices=["create_po", "post_invoice"], help="post a different amount here")
    p.add_argument("--task", default="R-0201")
    args = p.parse_args(argv)

    tmpdir = None
    path = args.ledger
    if not path:
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "ledger.jsonl")

    commissioner = Ledger.open(path, "commissioner")   # in the dispatcher
    harness = Ledger.open(path, "harness")             # in the tool executor only

    dispatch(commissioner, args.task, "buyer")
    diary = run_agent(args.task, "buyer", scripted_policy(args.skip, args.wrong_amount), ToolExecutor(harness))
    commissioner.close(args.task)                      # the dispatcher calls the task finished

    bal = commissioner.balance()
    print("agent diary:")
    for line in diary:
        print("  " + line)
    print()
    print(format_table(commissioner.trial_balance()))
    print()
    print(f"skipped {len(bal.skipped_steps)}  amount {len(bal.amount_mismatches)}  unowed {len(bal.unowed_runs)}  "
          f"chain {'verified' if commissioner.verify() else 'BROKEN'}  ledger {path if (args.ledger or args.keep) else '(temporary)'}")

    if tmpdir and not args.keep:
        for name in os.listdir(tmpdir):
            os.remove(os.path.join(tmpdir, name))
        os.rmdir(tmpdir)
    return 1 if bal.imbalances else 0


if __name__ == "__main__":
    raise SystemExit(main())
