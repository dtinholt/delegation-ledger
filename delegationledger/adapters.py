"""Two hooks that fit most agent frameworks.

``commission`` belongs where work is handed out: the dispatcher, the planner,
the orchestrator's task queue. It writes the obligation book before the agent
starts.

``observed`` wraps a tool function at the point where the framework actually
executes it. It writes the resource book after the call returns, from the
arguments the tool really received, so an agent that only says it called a tool
leaves no line.

Neither hook is handed to the agent. If the agent can reach the ledger object,
it can write its own obligations and its own runs, and the balance stops
meaning anything. See ADAPTERS.md for framework-specific placement.
"""
from __future__ import annotations

import functools

from .ledger import LedgerError, _check_name, _parse_time, norm_amount


def commission(ledger, task_id, agent, steps, *, by="commissioning"):
    """Write one obligation per step. ``steps`` is a list of step names or of
    dicts ``{"step": ..., "amount": ..., "due": ...}``. The whole plan is
    checked first, so a bad step writes nothing rather than half a plan."""
    plan = []
    for i, s in enumerate(steps):  # check the whole plan before writing any of it
        if isinstance(s, str):
            plan.append({"step": s})
        elif isinstance(s, dict) and isinstance(s.get("step"), str):
            plan.append(s)
        else:
            raise LedgerError(f"step {i} must be a step name or a dict with a 'step' name")
        norm_amount(plan[-1].get("amount"))
        _check_name("step", plan[-1]["step"])
        if plan[-1].get("due") is not None:
            _parse_time(plan[-1]["due"], "due")
    return [ledger.owe(task_id, agent, s["step"], amount=s.get("amount"), due=s.get("due"), by=by)
            for s in plan]


def observed(ledger, agent, step=None, *, task_arg="task_id", amount_arg=None, by="harness"):
    """Decorator for a tool function. After a call returns without raising, it
    records ``ran(task_id, agent, step, amount)``.

    ``task_arg`` names the keyword argument carrying the task id; ``amount_arg``
    names the one carrying the amount, if the step has one. A call that raises
    records nothing, because nothing ran to completion. If the tool succeeded
    and the ledger write then fails, the ledger error propagates: a run that
    could not be recorded is not hidden.
    """
    def wrap(fn):
        name = step or fn.__name__

        @functools.wraps(fn)
        def inner(*args, **kwargs):
            if task_arg not in kwargs:
                raise TypeError(f"{name}: pass {task_arg}= as a keyword so the ledger can see it")
            result = fn(*args, **kwargs)
            ledger.ran(kwargs[task_arg], agent, name,
                       amount=kwargs.get(amount_arg) if amount_arg else None, by=by)
            return result
        return inner
    return wrap
