"""Process entry points for the concurrency tests. They live in their own
module so a spawned child can import them by name."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from delegationledger import Ledger  # noqa: E402


def appender(path, writer, n, start_event=None):
    """Write n lines, alternating owe and ran, each through a fresh handle
    every tenth line so both the incremental and the full read paths run."""
    if start_event is not None:
        start_event.wait()
    led = Ledger(path)
    for i in range(n):
        if i % 10 == 0:
            led = Ledger(path)
        task = f"W{writer}-T{i // 2}"
        if i % 2 == 0:
            led.owe(task, f"agent{writer}", "step", amount=i, by=f"writer{writer}")
        else:
            led.ran(task, f"agent{writer}", "step", amount=i - 1, by=f"writer{writer}")
    return n


def rotator(path, times, start_event=None):
    import time
    if start_event is not None:
        start_event.wait()
    led = Ledger(path)
    done = 0
    for _ in range(times):
        time.sleep(0.01)
        try:
            led.rotate()
            done += 1
        except Exception as exc:  # an empty live file is the only expected refusal
            if "nothing to rotate" not in str(exc):
                raise
    return done
