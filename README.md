An agent's log records what it did and has no line for a step it skipped, so this stdlib-only Python library keeps a second, hash-chained book of what each agent owes and reports where it disagrees with what ran.

# The Delegation Ledger

**Status:** version 0.2.x, first public release 11 October 2026.

Lab page: https://dinand.com/experiments/delegation-ledger/

Source: https://github.com/dtinholt/delegation-ledger

License: Apache-2.0.

An agent's log records what it did. It has no line for a step it skipped. This library keeps a second book, filled in by the dispatcher with what each agent owes before it acts, and sets it against the runs your tool executor records, so you read only the lines where the two disagree.

It comes out of the Trial Balance campaign ([A Trial Balance for Agent Omissions](https://dinand.com/research/campaigns/trial-balance/), published 2026-10-01), which tested the same two books on a seven-step procurement workflow.

## What is in the folder

| path | what it is |
|---|---|
| `delegationledger/` | the library, Python standard library only |
| `delegationledger/ledger.py` | `Ledger`, the hash chain, rotation, locking, `balance()`, `trial_balance()` |
| `delegationledger/adapters.py` | `commission` for task dispatch, `observed` for tool calls |
| `delegationledger/cli.py` | the `delegationledger` command |
| `delegationledger/report.py` | static HTML report with agent, task and timeline views; CSV export |
| `delegationledger/watch.py` | tail a ledger and print new imbalances |
| `examples/agent_loop.py` | a generic agent loop wired to both handles, runnable without a model |
| `ADAPTERS.md` | where to put the two hooks in any agent framework |
| `CHANGELOG.md` | what the stress round broke and what changed |
| `tests/` | unittest suite, the headless page smoke test `smoke.sh` |
| `demo/index.html` | live view of a ledger file, or a dropped or pasted one |
| `demo/make_sample.py` | writes `demo/sample-ledger.jsonl`, its companions and `demo/fixtures/` |

## Use it

```python
from delegationledger import Ledger, format_table

led = Ledger("ledger.jsonl")

# when the work is assigned, before the agent starts
led.owe("R-0105", "buyer", "create_po", amount=5955, due="2026-10-01T17:00:00Z")
led.owe("R-0105", "buyer", "post_goods_receipt")

# from the layer that executed the tool call
led.ran("R-0105", "buyer", "create_po", amount=9555)

b = led.balance()
b.skipped_steps       # owed, not run, and no longer expected (rules under "What it does not do")
b.open_obligations    # owed, not run, not late yet
b.amount_mismatches   # ran at a different amount from the one owed
b.unowed_runs         # ran with nothing owed
b.blind_spot          # the sentence below, every time

print(format_table(led.trial_balance()))
```

A `Ledger` can write both books, which suits tests and single-process scripts. In a deployment, split it:

```python
from delegationledger import Ledger

commissioner = Ledger.open("ledger.jsonl", "commissioner")   # in the dispatcher: owe, close
harness = Ledger.open("ledger.jsonl", "harness")             # where tools run: ran

commissioner.owe("R-0110", "buyer", "post_goods_receipt")
harness.ran("R-0110", "buyer", "check_vendor")
harness.owe(...)               # raises RoleError
commissioner.close("R-0110")   # every step still owed on R-0110 now counts as skipped
```

Each line carries its role (`commissioner` on owe and close lines, `harness` on ran lines) and the hash covers it, so a ran line relabelled as an obligation breaks the chain. `by` stays a free-text field naming the writer.

From the shell:

```sh
pip install -e .                       # or: python3 -m delegationledger ...
delegationledger --ledger l.jsonl owe R-1 buyer create_po --amount 5955
delegationledger --ledger l.jsonl ran R-1 buyer create_po --amount 9555
delegationledger --ledger l.jsonl close R-1
delegationledger --ledger l.jsonl balance          # exit 1 when any line fails to balance
delegationledger --ledger l.jsonl balance --json
delegationledger --ledger l.jsonl report --html report.html
delegationledger --ledger l.jsonl export --csv tb.csv     # or --csv - for standard output
delegationledger --ledger l.jsonl watch --interval 2      # Ctrl-C stops
delegationledger --ledger l.jsonl rotate
delegationledger --ledger l.jsonl verify                  # every segment, then the head
```

`$DELEGATION_LEDGER` sets the default file. Exit codes: 0 fine, 1 `balance` found an imbalance, 2 no ledger file, 3 a ledger error such as a broken chain or a bad time.

To see the two handles in a working loop with no model behind it:

```sh
python3 examples/agent_loop.py                            # balances, exit 0
python3 examples/agent_loop.py --skip post_goods_receipt  # the agent claims the step; the balance shows it skipped
python3 examples/agent_loop.py --wrong-amount post_invoice
```

## Rotation

`rotate()` (or `delegationledger rotate`, from the commissioner side) appends a `seal` line naming the segment the file becomes, renames `ledger.jsonl` to `ledger.000001.jsonl`, and writes a new `ledger.jsonl` whose first line is a `carry` holding the seal's hash. `seq` runs on across files. Loading, `balance()`, `verify()` and the report read every segment, so an obligation in an old segment still meets its run in the live file. Deleting, renaming or editing a segment, or appending to a sealed one, fails verification and names the file and line. A crash between any two steps of a rotation is finished by the next append. Segment names in seal and carry lines must be bare names of the form `stem.NNNNNN.ext`, so a forged seal cannot point the rename at another directory. If `ledger.jsonl` is a symbolic link, `rotate()` renames the link itself, so the archived segment is that link, still pointing at the original file, and the new live file is a plain file in the link's directory. The series verifies; the original file keeps its name and stops growing. Point the library at the real path if that matters. A failed rotation leaves no `ledger.jsonl.tmp` behind. Segments must stay next to the live file; pruning old ones is not supported, because a series that starts mid-chain would look the same as one with its head cut off.

## Several writers

Appends and rotations take an exclusive `flock` on `ledger.jsonl.lock`; reads take a shared one, so a reader never sees a rotation half done. Each append reads only the bytes other writers added since its last read, and checks that the line it last saw is still in place. A test runs four processes appending at once, and another rotates while three append; the chain stays whole and every owe meets its ran. Windows has no `flock` here, so appends there are unlocked.

## The file

One JSON object per line, keys sorted. There are five kinds of line: `owe`, `close`, `seal` and `carry` (role `commissioner`) and `ran` (role `harness`). A line whose role does not match its kind fails verification, and so does a line with a missing or extra field, a field of the wrong type, or a name longer than 512 characters. These checks run before hashing, so a forger who recomputes the hashes still cannot slip in a float, a nested object or markup where an amount belongs. Each line carries `seq`, the hash of the previous line in `prev`, and its own SHA-256 in `hash`. Amounts are stored as plain decimal strings (no exponent, up to 30 integer and 18 decimal digits), so `120.5` and `120.50` compare equal, negative amounts work, and no float formatting touches the hash. Due times are stored in UTC; a time with no offset is read as UTC. Amounts compare one way round: an obligation written without an amount accepts a run at any amount, and an obligation written with one requires the run to carry an equal amount, so a run that posts none shows as an amount mismatch. The dispatcher decides which steps carry an amount by writing one or not. `owe` and `ran` refuse a missing or blank agent or step; only a `close` line carries neither, so a handle cannot write a line the loader would refuse. A write that fails part way, for a full disk or a file-size limit, cuts the file back to its last whole line before the error reaches you. Loading a file verifies the whole chain and names the first line that fails. Appends take a POSIX file lock (none on Windows) and fsync each line. Names may hold any Unicode, including U+2028 and NEL; files are split on newline only.

A file whose last line has no newline holds a partial write, the kind a crash mid-append leaves. Reading it or appending to it raises `ChainError` naming that line and telling you to inspect it and truncate the partial tail by hand. The library makes no guess at a repair, since a guessed line would enter the books unverified. `verify()` re-reads the file each time and returns False on any break.

## Run the tests and the demo

```sh
python3 -W error -m unittest discover -s tests -v
sh tests/smoke.sh                      # headless Chrome; set CHROME= if it lives elsewhere
python3 demo/make_sample.py
open demo/index.html                   # or serve the folder: python3 -m http.server -d demo
```

The suite has 111 tests and runs clean under `-W error` in about two seconds. One test per fault condition from the campaign:

| condition | what the balance does |
|---|---|
| no fault | balances |
| skipped step | reports it as skipped |
| skipped step, the agent's diary says it was done | reports it as skipped; the diary never reaches the ledger |
| wrong amount | reports an amount mismatch with both values |
| wrong amount on both sides | balances, and the test asserts that it does |

The rest cover the role handles (each refuses the other book's verb, `hasattr` reports False for it, and the chain covers the role), closing a task, and the chain. An edit or a deletion breaks the chain, and so does a partial last line. Truncating the tail goes unnoticed by the chain itself; the test shows the head hash changes, which is why you anchor a copy of it elsewhere. Other tests in `test_ledger.py` separate open steps from skipped ones and check overdue steps, unowed runs and agents kept apart. The CLI, the HTML report and the adapter hooks have their own.

Newer suites:

- `test_concurrency.py`: two and four processes appending at once (spawned, released together by one event; the test asserts their lines really interleave), and appends during rotation.
- `test_rotation.py`: seal and carry contents, verify and balance across segments, each tampering case, both crash windows, a reader following another process's rotation.
- `test_properties.py`: 60 seeded random owe, ran and close sequences against a reference model written from the rules in this README; every ran without an owe is unowed; owed and run at equal amounts, however written, balances; closing marks exactly the still-owed steps and changes nothing else; counts are conserved; rotation at random points leaves the trial balance unchanged.
- `test_hostile.py`: the stress inputs listed in `CHANGELOG.md`, the DST changes and the year boundary in Chicago, and 12,200 appends through one file.
- `test_tools.py`: the report's views, CSV, watch, the CLI's newer commands, the example.

The page checks itself in the console: 27 checks, among them the balance and chain head against Python on the sample, on a 300-step random ledger and on a rotated series handed over out of order; Unicode names hashing to Python's bytes; markup in every field rendering as text; a forged amount refused; a missing middle segment caught. `tests/smoke.sh` loads the page headless, asserts all 27 pass from disk and over http, opens the hostile and rotated fixtures, checks that off-site, `data:`, space- or tab-prefixed and backslash `?ledger=` values are refused with a visible warning, and times the load.

The demo page reads `sample-ledger.jsonl` when served over http and the inlined copy in `sample-ledger.js` when opened from disk. It verifies the hash chain in the browser, recomputes the balance in JavaScript, and takes any other ledger by file picker, drag and drop, or paste. Drop all the segments of a rotated ledger at once and it orders them, checks each link and shows the seal heads. One later segment on its own verifies from its carry head, and the page says the earlier files are not loaded. On a broken chain it names the file and line and shows only the lines before the break. Below the trial balance come the views per agent and per task, a timeline per task, and a panel on what each column means.

Measured in headless Chrome on the build machine, wall clock from reading the text to the end of the render, `tests/smoke.sh`:

| input | lines | load, ms | of which render, ms |
|---|---|---|---|
| the sample | 122 | 4 | 2 |
| 10 times the sample | 1,220 | 23 | 7 |
| 100 times the sample, 4.4 MB | 12,200 | 161 | 23 |
| a 5.1 MB paste (116 times the sample) | 14,152 | 671 | 26 |

The tables show the first 400 lines and 300 tasks, and the timeline the first 40 tasks, each with a button for the rest. Past 1,000 lines the status line shows chain-check progress. In Python the 100x file appends in 1.07 s, loads and verifies in 0.08 s, balances in 0.02 s and renders its report in 0.15 s. When all 27 console checks pass, the page sets `data-selfcheck="pass"` on its root element. The sample is a simulation from published coefficients, not a deployed system: its amounts and times are made up.

## What it does not do

It finds omissions and one-sided mismatches. A wrong amount carried on both sides is invisible to it by construction. If the error entered before the work was assigned, the obligation already holds the wrong value, the run matches it, and the line balances. In the campaign the balance check found 0 of 1,044 of these. A full read of the log found them almost every time, because the vendor's quote still showed the true amount.

The balance is only as honest as whoever writes the two books. The role handles stop a harness handle from writing obligations and a commissioner handle from writing runs. They live in the same Python process as whatever holds them, so they cannot stop code that opens the file itself or builds a full `Ledger`. Give the agent process only the harness handle and keep the commissioner handle in the dispatcher; `ADAPTERS.md` covers placement.

The chain catches edits and deletions inside the file. Cut lines off the end and the shorter file verifies without complaint. A copy of the head hash, kept somewhere the agent cannot write, is what anchors the tail.

`watch` polls; it does not use file-system events, and its interval sets how late a new imbalance can show. The page keeps files in the tab and stores nothing. Rotation needs every segment kept beside the live file.

It checks counts and amounts per (task, agent, step). Step order, the time between steps and the correctness of a step's output are outside it.

A step counts as skipped once the commissioner closes its task, a later owed step on the same task and agent has run, or its due time has passed. A task with no close and no due times stays open until you close it or give the step a due time; the library sets no deadline of its own.

Every figure quoted here comes from the campaign's audits of its own procurement workflow, not from this library running against a live system.

## The campaign figures this build quotes

| figure | what it is | source |
|---|---|---|
| 0 of 1,044 | wrong amounts carried on both sides that the balance check found | campaign page `site/content/campaigns/trial-balance.md`, found section |
| 0.80 at 745 prompt tokens | omission detection by reading the imbalance first, eight requisitions; 90% interval 740 to 749 | `site/content/claims.yaml` tb-c80 |
| 0.58 | best omission detection by plain log review at any budget or length | `site/content/claims.yaml` tb-lr-best |
| about 21 times (1.34 against 0.063) | correct omission findings per thousand tokens read, imbalance-first over log review at its best budget | `site/content/claims.yaml` tb-ratio |
| 18,360 | audits in the campaign, one auditor model | `site/content/campaigns.yaml` trial-balance |
| seven steps, approval above 5,000 | the workflow the tests and the demo rebuild | campaign pre-registration, section 3 |

## Sources

- Dinand Tinholt, Research. "A Trial Balance for Agent Omissions", campaign page and article, published 2026-10-01. https://dinand.com/research/campaigns/trial-balance/
- The concept page "Trial Balance" on the same site.
- https://dinand.com/research/

Apache-2.0. See `LICENSE`.
