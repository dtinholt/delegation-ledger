"""Tail a ledger file and print imbalances as they appear.

Polling only, standard library only. Each poll reads the bytes other writers
added since the last one, recomputes the balance and prints the lines that
are new since the previous poll, plus the ones that have since cleared."""
from __future__ import annotations

import sys
import time

from .ledger import ChainError, Ledger, printable

WHY = {"closed": "task closed", "moved_on": "a later owed step ran", "overdue": "past its due time"}


def _items(bal):
    out = {}
    for s in bal.skipped_steps:
        out[("skipped", s["task_id"], s["agent"], s["step"])] = f"owed {s['owed']}, ran {s['ran']}; {WHY.get(s['reason'], '')}"
    for m in bal.amount_mismatches:
        out[("amount", m["task_id"], m["agent"], m["step"])] = (
            f"owed {', '.join(str(a) for a in m['owed_amounts'])}, ran {', '.join(str(a) for a in m['ran_amounts'])}")
    for u in bal.unowed_runs:
        out[("unowed", u["task_id"], u["agent"], u["step"])] = f"ran {u['ran']} against {u['owed']} owed"
    return out


def watch(path, interval=2.0, iterations=None, out=None, as_of=None, sleep=time.sleep, now=None) -> int:
    """Poll ``path`` every ``interval`` seconds and print each new imbalance
    once. ``iterations`` stops after that many polls (None runs until
    interrupted). ``as_of`` fixes the balance time, for tests; by default each
    poll uses the current time, so a due time passing shows up. Returns the
    number of imbalance lines printed."""
    out = out or sys.stdout
    stamp = now or (lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    led, seen, broken, printed, n = None, {}, None, 0, 0
    while iterations is None or n < iterations:
        n += 1
        try:
            if led is None:
                led = Ledger(path)
            else:
                led._sync()
            current = _items(led.balance(as_of))
            if broken:
                print(f"{stamp()}  chain readable again", file=out)
                broken = None
        except ChainError as exc:
            if str(exc) != broken:
                print(f"{stamp()}  CHAIN  {printable(exc)}", file=out)
                broken = str(exc)
            led, current = None, seen
        else:
            label = "now" if n == 1 else "new"
            for key, detail in current.items():
                if key not in seen:
                    status, task, agent, step = key
                    print(f"{stamp()}  {label:<7}{status:<8}{printable(task)}  {printable(agent)}  {printable(step)}  "
                          f"{printable(detail)}", file=out)
                    printed += 1
            for key in seen:
                if key not in current:
                    status, task, agent, step = key
                    print(f"{stamp()}  cleared {status:<8}{printable(task)}  {printable(agent)}  {printable(step)}", file=out)
            if n == 1 and not current:
                print(f"{stamp()}  watching {printable(path)}: {len(led.entries)} entries, nothing out of balance", file=out)
        seen = current
        out.flush()
        if iterations is None or n < iterations:
            sleep(interval)
    return printed
