# Wiring the ledger into an agent framework

Every agent framework has two places that matter here. One hands work to an agent. The other executes a tool call the agent asked for. The obligation goes in at the first place and the run goes in at the second, and both sit outside the agent's own code.

## The rule that makes the balance mean something

The writer of `owe` and the writer of `ran` sit outside the agent. If the agent can call `ledger.ran(...)`, it can post a run it never made, and a skipped step disappears from the balance. In the Trial Balance campaign the obligation book took posts only from the commissioning layer, the resource book only from the harness, and the agent's tool table had no ledger tool in it.

The library splits the two books with role handles:

```python
commissioner = Ledger.open("ledger.jsonl", "commissioner")   # owe, close
harness = Ledger.open("ledger.jsonl", "harness")             # ran
```

A harness handle that calls `owe` or `close` raises `RoleError`, and so does a commissioner handle that calls `ran`. Every line records its role and the hash chain covers it.

The commissioner handle lives in the dispatcher. The process that runs the agent gets only the harness handle, and only inside the tool executor, kept out of the agent's tools and its prompt context. A handle is a Python object, so the split holds only as far as process boundaries hold: an agent that can open the file directly, or construct a full `Ledger`, can still write either book. Run the dispatcher in its own process, or behind its own service, when that matters. `by` remains a free-text field for naming the writer inside a role.

## Task dispatch: write what is owed

Find the point where a task becomes an assignment, such as a planner emitting steps or a person approving a plan. Call `commission` there, before the agent starts.

```python
from delegationledger import Ledger, commission

commissioner = Ledger.open("ledger.jsonl", "commissioner")

def dispatch(task_id, agent_name, plan):
    commission(commissioner, task_id, agent_name, [
        {"step": s.tool, "amount": s.expected_amount, "due": s.deadline} for s in plan
    ])
    agent_queue.put((task_id, agent_name, plan))
```

The obligation should come from a source the dispatcher trusts, such as the order or the approved plan. If it comes from the agent's own plan, the agent is writing its own obligations and the balance can only catch it contradicting itself.

## Tool execution: write what ran

Find the function that actually runs a tool after the model asks for it: a tool executor, a function-calling loop, a middleware layer, an MCP client. Record the run there, after the tool returns, from the arguments the tool really received.

As a decorator on your own tools:

```python
from delegationledger import Ledger, observed

harness = Ledger.open("ledger.jsonl", "harness")

@observed(harness, agent="payables", amount_arg="amount_usd")
def post_invoice(*, task_id, po_id, amount_usd):
    ...
```

As a generic hook in an executor loop:

```python
def execute_tool_call(task_id, agent_name, call):
    result = TOOLS[call.name](**call.arguments)       # raises on failure: nothing recorded
    harness.ran(task_id, agent_name, call.name, amount=call.arguments.get("amount_usd"))
    return result
```

Most frameworks expose a callback or event for "tool finished". Use that one. "Tool requested" fires before anything ran, and the agent's text output is a claim. Neither belongs in the resource book.

## Closing a task

A missing run becomes a skipped step in three ways: a later owed step on the same task and agent ran, a due time passed, or the commissioner closed the task. `commissioner.close(task_id)` writes a close line, and every step still owed on that task counts as skipped from then on. Close a task when the dispatcher gives up on it or calls it done. A task with no close and no due times stays open until one of those three things happens.

## Several agents

Each (task, agent, step) is its own account. A planner that owes `plan` and an executor that runs `plan` show as one open obligation and one unowed run. Hand-offs between agents show up on the balance this way. A team of ten agents can post into one file. Appends take an exclusive POSIX lock on `ledger.jsonl.lock` (none on Windows) and pick up whatever other writers added before writing, so two writers cannot fork the chain; reads take a shared lock. The library tests this with four processes writing at once.

## Steps the dispatcher does not know about

Some steps only become known during the work, such as a branch that depends on a lookup. The campaign's approval step was owed only above 5,000, and the dispatcher knew the amount up front. When the dispatcher cannot know, have the deciding component write the new obligation as soon as the branch is taken, and keep that component outside the agent.

## A complete loop to copy

`examples/agent_loop.py` puts the pieces in one file: a dispatcher holding the commissioner handle, a tool executor holding the harness handle, and a scripted policy standing in for the model. The policy can skip a step while claiming it in its diary, or post a different amount, and the balance shows both. Replace `scripted_policy` with your model call and `TOOLS` with your tools.

## Rotating a long-running ledger

Call `commissioner.rotate()` on whatever schedule suits, from the dispatcher side. The live file is sealed and moved to `ledger.000001.jsonl`, and a new live file opens with a line carrying the sealed head. Writers holding handles need no restart: their next append follows the rotation. Keep every segment; the balance reads them all, and the chain refuses a series with one missing. The sealed head is a natural value to copy somewhere the agent cannot write.

## Watching

`delegationledger watch` polls the file and prints each new imbalance once, with a note when one clears and a single line if the chain breaks. Run it beside the dispatcher, or point a log shipper at its output.

## Anchoring the head

The hash chain catches edits and deletions in the middle of the file. Truncating the tail goes unnoticed: the shorter chain verifies. Copy `ledger.head` somewhere the agent cannot write, a log line or a database row, and compare it on read when that matters.
