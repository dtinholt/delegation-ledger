"""delegationledger CLI.

    delegationledger [--ledger FILE] owe TASK AGENT STEP [--amount N] [--due ISO]
    delegationledger [--ledger FILE] ran TASK AGENT STEP [--amount N]
    delegationledger [--ledger FILE] close TASK
    delegationledger [--ledger FILE] rotate
    delegationledger [--ledger FILE] balance [--json] [--as-of ISO]
    delegationledger [--ledger FILE] report --html OUT [--as-of ISO]
    delegationledger [--ledger FILE] export --csv OUT|- [--as-of ISO]
    delegationledger [--ledger FILE] watch [--interval S] [--count N]
    delegationledger [--ledger FILE] verify

The ledger file defaults to $DELEGATION_LEDGER, then ./ledger.jsonl.
``balance`` exits 0 when nothing is skipped, mismatched or unowed, 1 otherwise.
Exit 2: no ledger file. Exit 3: a ledger error (broken chain, bad input).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .ledger import Ledger, LedgerError, format_table, printable, segment_paths, verify_series
from . import report
from .watch import watch


def _parser():
    p = argparse.ArgumentParser(prog="delegationledger",
                                description="Record what agents owe and what ran; report where the books disagree.")
    p.add_argument("--ledger", default=os.environ.get("DELEGATION_LEDGER", "ledger.jsonl"),
                   help="ledger file (default: $DELEGATION_LEDGER or ./ledger.jsonl)")
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("owe", help="record an obligation, before the agent acts")
    o.add_argument("task_id"); o.add_argument("agent"); o.add_argument("step")
    o.add_argument("--amount"); o.add_argument("--due", help="ISO 8601 time; UTC if no offset")
    o.add_argument("--by", default="commissioning", help="who is writing (default: commissioning)")

    r = sub.add_parser("ran", help="record a step that actually ran")
    r.add_argument("task_id"); r.add_argument("agent"); r.add_argument("step")
    r.add_argument("--amount")
    r.add_argument("--by", default="harness", help="who is writing (default: harness)")

    c = sub.add_parser("close", help="close a task: every step still owed on it counts as skipped")
    c.add_argument("task_id")
    c.add_argument("--by", default="commissioning", help="who is writing (default: commissioning)")

    ro = sub.add_parser("rotate", help="seal the live file and start a new one that carries its head")
    ro.add_argument("--by", default="commissioning", help="who is writing (default: commissioning)")

    b = sub.add_parser("balance", help="print the trial balance and the imbalances")
    b.add_argument("--json", action="store_true"); b.add_argument("--as-of")

    h = sub.add_parser("report", help="write a static HTML report")
    h.add_argument("--html", required=True, metavar="OUT"); h.add_argument("--as-of")
    h.add_argument("--title", default="Delegation ledger report")

    x = sub.add_parser("export", help="write the trial balance as CSV")
    x.add_argument("--csv", required=True, metavar="OUT", help="file to write, or - for standard output")
    x.add_argument("--as-of")

    w = sub.add_parser("watch", help="tail the file and print new imbalances (Ctrl-C stops)")
    w.add_argument("--interval", type=float, default=2.0, help="seconds between polls (default 2)")
    w.add_argument("--count", type=int, default=None, help="stop after this many polls")

    sub.add_parser("verify", help="check the hash chain across every segment")
    return p


def _exists(path):
    return os.path.exists(path) or bool(segment_paths(path))


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.cmd in ("balance", "report", "verify", "export", "rotate") and not _exists(args.ledger):
            print(f"no ledger at {printable(args.ledger)}", file=sys.stderr)
            return 2
        if args.cmd == "watch":
            if args.interval <= 0:
                print("error: --interval must be positive", file=sys.stderr)
                return 3
            try:
                watch(args.ledger, interval=args.interval, iterations=args.count)
            except KeyboardInterrupt:
                pass
            return 0
        role = {"owe": "commissioner", "close": "commissioner", "rotate": "commissioner", "ran": "harness"}.get(args.cmd)
        led = Ledger.open(args.ledger, role) if role else Ledger(args.ledger)
        if args.cmd == "close":
            e = led.close(args.task_id, by=args.by)
            print(f"close #{e['seq']} {e['hash'][:12]}")
        elif args.cmd == "owe":
            e = led.owe(args.task_id, args.agent, args.step, amount=args.amount, due=args.due, by=args.by)
            print(f"owe #{e['seq']} {e['hash'][:12]}")
        elif args.cmd == "ran":
            e = led.ran(args.task_id, args.agent, args.step, amount=args.amount, by=args.by)
            print(f"ran #{e['seq']} {e['hash'][:12]}")
        elif args.cmd == "rotate":
            r = led.rotate(by=args.by)
            print(f"sealed {r['segment']} at head {r['head'][:12]}; {os.path.basename(args.ledger)} "
                  f"opens with carry #{r['carry']['seq']}")
        elif args.cmd == "verify":
            s = verify_series(args.ledger)
            for seg in s["segments"]:
                print(f"  {printable(seg['file'])}: {seg['lines']} lines, head {seg['head'][:12]}")
            print(f"chain verified: {s['entries']} entries in {len(s['segments'])} file(s), head {s['head'][:12]}")
        elif args.cmd == "balance":
            bal = led.balance(args.as_of)
            if args.json:
                print(json.dumps({"trial_balance": led.trial_balance(args.as_of), "balance": bal.to_dict()}, indent=2))
            else:
                print(format_table(led.trial_balance(args.as_of)))
                print()
                print(f"skipped {len(bal.skipped_steps)}  amount {len(bal.amount_mismatches)}  "
                      f"unowed {len(bal.unowed_runs)}  open {len(bal.open_obligations)}  as of {bal.as_of}")
                print("blind spot: " + bal.blind_spot)
            return 1 if bal.imbalances else 0
        elif args.cmd == "report":
            page = report.render(led, args.as_of, args.title)
            with open(args.html, "w", encoding="utf-8") as fh:
                fh.write(page)
            print(f"wrote {printable(args.html)}")
        elif args.cmd == "export":
            if args.csv == "-":
                report.write_csv(led, sys.stdout, args.as_of)
            else:
                with open(args.csv, "w", encoding="utf-8", newline="") as fh:
                    n = report.write_csv(led, fh, args.as_of)
                print(f"wrote {n} rows to {printable(args.csv)}")
    except LedgerError as exc:
        print(f"error: {printable(exc)}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"error: {printable(exc)}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
