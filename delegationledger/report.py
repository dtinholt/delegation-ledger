"""Static HTML report and CSV export of a ledger.

The report is one self-contained file in the lab's ledger look: the imbalances
first, then the trial balance, a view per agent and per task, a timeline of
owe and ran lines for each task, the segments of a rotated series, what each
column means, and the blind-spot note."""
from __future__ import annotations

import csv
import html
from collections import OrderedDict, defaultdict

from .ledger import BLIND_SPOT, COLUMNS, ChainError, LedgerError, _parse_time, verify_series

STATUSES = ("ok", "open", "skipped", "amount", "unowed")
BAD = {"skipped", "amount", "unowed"}
TIMELINE_CAP = 60  # tasks drawn; tasks with an imbalance come first

COLUMN_NOTES = OrderedDict([
    ("task", "The task id the dispatcher assigned. One task can involve several agents."),
    ("agent", "The agent that owed the step or ran it. Each agent is its own account."),
    ("step", "The step or tool name. A (task, agent, step) is one line of the balance."),
    ("owed", "How many times the commissioner wrote that this step is owed."),
    ("ran", "How many times the harness recorded the step running."),
    ("owed amount", "The amount on each obligation, where the step carries one."),
    ("ran amount", "The amount on each run. Compared with the owed amount as decimals."),
    ("status", "ok: both books agree. open: owed, not run, not late yet. skipped: owed, not run, "
               "and the task was closed, a later owed step ran, or the due time passed. amount: ran at a "
               "different amount. unowed: ran more often than it was owed."),
    ("reason", "Why a skipped step counts as skipped: closed, moved_on or overdue."),
    ("due", "The earliest due time on the obligation, in UTC. A step with no due time and no close stays open."),
])

CSS = """
:root{--paper:#f3f0e6;--ink:#1c1d19;--graphite:#5d5f55;--rule:#d3cdb9;--ledger:#1d4a6b;--stamp:#a3361d;
--display:"Newsreader",Georgia,serif;--body:"Public Sans",system-ui,sans-serif;--mono:"IBM Plex Mono",ui-monospace,Menlo,monospace}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--paper:#171814;--ink:#e7e3d5;--graphite:#a09f93;--rule:#3a3a32;--ledger:#93bcdc;--stamp:#e58d6c}}
:root[data-theme="dark"]{--paper:#171814;--ink:#e7e3d5;--graphite:#a09f93;--rule:#3a3a32;--ledger:#93bcdc;--stamp:#e58d6c}
*{box-sizing:border-box}html{background:var(--paper)}
body{margin:0;background:var(--paper);color:var(--ink);font:400 1rem/1.6 var(--body);font-variant-numeric:tabular-nums;overflow-x:hidden}
a{color:var(--ledger)}
.frame{max-width:64rem;margin:0 auto;padding:0 16px}
header{border-top:4px solid var(--ink);border-bottom:3px double var(--ink);padding:1rem 0 1.25rem}
.brand{font-family:var(--display);font-size:1.3rem;font-weight:500;margin:0 0 .4rem}
h1,h2{font-family:var(--display);font-weight:500;margin:0}h1{font-size:2rem;line-height:1.1}
h2{font-size:1.3rem;margin:2.2rem 0 .5rem;border-bottom:1px solid var(--ink);padding-bottom:.3rem}
.mono{font-family:var(--mono);font-size:.78rem;color:var(--graphite);overflow-wrap:anywhere}
.wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:.9rem}
caption{text-align:left;font-family:var(--mono);font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;color:var(--graphite);padding:.2rem 0 .4rem}
th,td{text-align:left;padding:.4rem .6rem .4rem 0;border-bottom:1px solid var(--rule);white-space:nowrap}
th[scope=row]{font:inherit;text-transform:none;letter-spacing:0;color:var(--ink);border-bottom-color:var(--rule)}
th{font-family:var(--mono);font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;color:var(--graphite);font-weight:400;border-bottom-color:var(--ink)}
td.n{font-family:var(--mono);text-align:right}th.n{text-align:right}
.st{font-family:var(--mono);font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;border:1.5px solid currentColor;border-radius:2px;padding:0 .35rem;color:var(--graphite)}
.st-bad{color:var(--ledger);border-style:double;border-width:3px}
.tally{display:grid;grid-template-columns:repeat(4,1fr);border-bottom:1px solid var(--rule);margin-top:1rem}
.tally div{padding:.8rem .5rem .8rem 0}.tally b{display:block;font-family:var(--mono);font-weight:500;font-size:1.7rem;line-height:1}
.tally span{font-size:.85rem;color:var(--graphite)}
ul{padding-left:1.1rem}li{margin:.3rem 0;overflow-wrap:anywhere}
.note{border-left:3px solid var(--stamp);padding-left:.8rem;max-width:66ch}
dl.cols{display:grid;grid-template-columns:9rem 1fr;gap:.35rem 1rem;margin:0}
dl.cols dt{font-family:var(--mono);font-size:.75rem;text-transform:uppercase;letter-spacing:.06em;color:var(--graphite)}
dl.cols dd{margin:0;max-width:62ch}
.tl{margin:.9rem 0 0;border-bottom:1px solid var(--rule);padding-bottom:.6rem}
.tl p{margin:0}.tl svg{display:block;width:100%;height:auto;max-width:44rem}
.tl .owe{fill:none;stroke:var(--ink);stroke-width:1.4}.tl .ran{fill:var(--ink)}
.tl .bad{stroke:var(--ledger);fill:var(--ledger)}.tl .owe.bad{fill:none;stroke-width:2.6}.tl .ran.bad{stroke:var(--ink);stroke-width:1.5}
.tl .close{stroke:var(--graphite);stroke-width:2}.tl .axis{stroke:var(--rule);stroke-width:1}
.tl text{font-family:var(--mono);font-size:10px;fill:var(--graphite)}
.legend{font-family:var(--mono);font-size:.75rem;color:var(--graphite)}
@media (max-width:640px){.tally{grid-template-columns:repeat(2,1fr)}dl.cols{grid-template-columns:1fr}dl.cols dd{margin-bottom:.5rem}}
@media print{.wrap{overflow:visible}th,td{white-space:normal}h2{break-after:avoid}.tl{break-inside:avoid}
dl.cols dt{break-after:avoid}dl.cols dd{break-before:avoid;break-inside:avoid}}
"""


def _e(x):
    return html.escape("" if x is None else str(x), quote=True)


def _stamp(status):
    return f'<span class="st{" st-bad" if status in BAD else ""}">{_e(status)}</span>'


def by_agent(rows) -> list[dict]:
    """One dict per agent: lines, owed, ran and a count per status."""
    acc = OrderedDict()
    for r in sorted(rows, key=lambda r: r["agent"]):
        a = acc.setdefault(r["agent"], dict({"agent": r["agent"], "lines": 0, "owed": 0, "ran": 0, "tasks": set()},
                                            **{s: 0 for s in STATUSES}))
        a["lines"] += 1
        a["owed"] += r["owed"]
        a["ran"] += r["ran"]
        a[r["status"]] += 1
        a["tasks"].add(r["task_id"])
    out = []
    for a in acc.values():
        a["tasks"] = len(a["tasks"])
        out.append(a)
    return out


def by_task(rows, entries) -> list[dict]:
    """One dict per task: agents, lines, a count per status, whether it was
    closed, and its first and last timestamps."""
    closed, first, last = set(), {}, {}
    for e in entries:
        t = e.get("task_id")
        if e["book"] not in ("owe", "ran", "close"):
            continue
        if e["book"] == "close":
            closed.add(t)
        first.setdefault(t, e["ts"])
        last[t] = e["ts"]
    acc = OrderedDict()
    for r in rows:
        t = acc.setdefault(r["task_id"], dict({"task_id": r["task_id"], "agents": set(), "lines": 0},
                                              **{s: 0 for s in STATUSES}))
        t["agents"].add(r["agent"])
        t["lines"] += 1
        t[r["status"]] += 1
    out = []
    for t in acc.values():
        t["agents"] = sorted(t["agents"])
        t["closed"] = t["task_id"] in closed
        t["first_ts"], t["last_ts"] = first.get(t["task_id"], ""), last.get(t["task_id"], "")
        out.append(t)
    return out


def timeline(entries, task_id) -> list[dict]:
    """The owe, ran and close lines of one task in file order."""
    return [{"seq": e["seq"], "book": e["book"], "agent": e.get("agent"), "step": e.get("step"),
             "amount": e.get("amount"), "ts": e["ts"]}
            for e in entries if e["book"] in ("owe", "ran", "close") and e.get("task_id") == task_id]


def _x_positions(events, x0, x1):
    """Time-scaled positions when every ts parses and the span is non-zero,
    otherwise evenly spaced in file order."""
    try:
        times = [_parse_time(e["ts"]).timestamp() for e in events]
    except LedgerError:
        times = None
    if times and max(times) > min(times):
        lo, hi = min(times), max(times)
        return [x0 + (t - lo) / (hi - lo) * (x1 - x0) for t in times], True
    n = len(events)
    return [x0 + (i / (n - 1) if n > 1 else 0.5) * (x1 - x0) for i in range(n)], False


def timeline_svg(task_id, events, bad_steps) -> str:
    """An inline SVG strip: owe as an open square above the rule, ran as a
    filled circle below it, close as a bar across it. Shapes carry the meaning;
    the accent marks steps in an imbalance, and each mark has a title."""
    W, H, x0, x1 = 640, 56, 8, 632
    xs, timed = _x_positions(events, x0, x1)
    marks = [f'<line class="axis" x1="{x0}" y1="24" x2="{x1}" y2="24"/>']
    for e, x in zip(events, xs):
        bad = (e["agent"], e["step"]) in bad_steps
        cls = " bad" if bad else ""
        label = _e(" ".join(str(x) for x in (e["book"], e["agent"], e["step"], e["amount"]) if x is not None)
                   + f' at {e["ts"]} (line {e["seq"]})')
        if e["book"] == "owe" and bad:   # out of balance: a diamond with a heavy stroke, not colour alone
            marks.append(f'<polygon class="owe bad" points="{x:.1f},5.5 {x + 5.5:.1f},11 {x:.1f},16.5 {x - 5.5:.1f},11">'
                         f'<title>{label}</title></polygon>')
        elif e["book"] == "owe":
            marks.append(f'<rect class="owe" x="{x - 3.5:.1f}" y="8" width="7" height="7"><title>{label}</title></rect>')
        elif e["book"] == "ran":
            marks.append(f'<circle class="ran{cls}" cx="{x:.1f}" cy="33.5" r="{5.2 if bad else 3.8}"><title>{label}</title></circle>')
        else:
            marks.append(f'<line class="close" x1="{x:.1f}" y1="4" x2="{x:.1f}" y2="44"><title>{label}</title></line>')
    if events:
        marks.append(f'<text x="{x0}" y="54">{_e(events[0]["ts"])}</text>')
        if len(events) > 1:
            marks.append(f'<text x="{x1}" y="54" text-anchor="end">{_e(events[-1]["ts"])}</text>')
    owes = sum(1 for e in events if e["book"] == "owe")
    rans = sum(1 for e in events if e["book"] == "ran")
    aria = _e(f"{task_id}: {owes} owe lines, {rans} ran lines, {len(bad_steps)} steps out of balance, "
              + ("spaced by time" if timed else "spaced in file order"))
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{aria}" xmlns="http://www.w3.org/2000/svg">'
            + "".join(marks) + "</svg>")


def render(ledger, as_of=None, title="Delegation ledger report") -> str:
    rows = ledger.trial_balance(as_of)
    bal = ledger.balance(as_of)
    entries = ledger.entries
    chain_ok = ledger.verify()
    segments = ledger.segments

    trs = []
    for r in rows:
        trs.append(
            "<tr>" + "".join(f"<td>{_e(r[c])}</td>" for c in ("task_id", "agent", "step"))
            + "".join(f'<td class="n">{_e(r[c])}</td>' for c in ("owed", "ran", "owed_amount", "ran_amount"))
            + f'<td>{_stamp(r["status"])}</td><td>{_e(r["reason"])}</td><td class="mono">{_e(r["due"])}</td></tr>')

    items = []
    for s in bal.skipped_steps:
        why = {"closed": "task closed", "moved_on": "a later step ran", "overdue": "past due"}.get(s.get("reason"), "")
        items.append(f"<li>Skipped: <b>{_e(s['step'])}</b> on {_e(s['task_id'])} by {_e(s['agent'])}, "
                     f"owed {_e(s['owed'])}, ran {_e(s['ran'])}; {_e(why)}</li>")
    for m in bal.amount_mismatches:
        items.append(f"<li>Amount: <b>{_e(m['step'])}</b> on {_e(m['task_id'])} by {_e(m['agent'])}, owed "
                     f"{_e(', '.join(str(a) for a in m['owed_amounts']))}, ran {_e(', '.join(str(a) for a in m['ran_amounts']))}</li>")
    for u in bal.unowed_runs:
        items.append(f"<li>Unowed: <b>{_e(u['step'])}</b> on {_e(u['task_id'])} by {_e(u['agent'])} ran "
                     f"{_e(u['ran'])} time(s) against {_e(u['owed'])} owed</li>")
    for o in bal.open_obligations:
        items.append(f"<li>Open, not yet late: {_e(o['step'])} on {_e(o['task_id'])} by {_e(o['agent'])}"
                     + (f", due {_e(o['due'])}" if o.get("due") else "") + "</li>")
    imbal = "<ul>" + "".join(items) + "</ul>" if items else "<p>The two books agree on every line.</p>"

    tally = "".join(f"<div><b>{n}</b><span>{_e(label)}</span></div>" for n, label in (
        (len(bal.skipped_steps), "skipped steps"), (len(bal.amount_mismatches), "amount mismatches"),
        (len(bal.unowed_runs), "runs nobody owed"), (len(bal.open_obligations), "open, not yet late")))

    agent_rows = "".join(
        f"<tr><th scope=\"row\">{_e(a['agent'])}</th>" + "".join(f'<td class="n">{a[k]}</td>' for k in
                                                   ("tasks", "lines", "owed", "ran") + STATUSES) + "</tr>"
        for a in by_agent(rows))
    tasks = by_task(rows, entries)
    task_rows = "".join(
        f"<tr><th scope=\"row\">{_e(t['task_id'])}</th><td>{_e(', '.join(t['agents']))}</td>"
        + "".join(f'<td class="n">{t[k]}</td>' for k in ("lines",) + STATUSES)
        + f"<td>{'yes' if t['closed'] else 'no'}</td><td class=\"mono\">{_e(t['first_ts'])}</td>"
          f"<td class=\"mono\">{_e(t['last_ts'])}</td></tr>"
        for t in tasks)

    bad_by_task = defaultdict(set)
    for r in rows:
        if r["status"] in BAD:
            bad_by_task[r["task_id"]].add((r["agent"], r["step"]))
    order = sorted(tasks, key=lambda t: (t["task_id"] not in bad_by_task,))
    drawn = order[:TIMELINE_CAP]
    events_by_task = defaultdict(list)
    for e in entries:
        if e["book"] in ("owe", "ran", "close"):
            events_by_task[e["task_id"]].append({"seq": e["seq"], "book": e["book"], "agent": e.get("agent"),
                                                 "step": e.get("step"), "amount": e.get("amount"), "ts": e["ts"]})
    tls = "".join(
        f'<div class="tl"><p class="mono">{_e(t["task_id"])} · {len(events_by_task[t["task_id"]])} entries'
        f'{" · " + str(len(bad_by_task[t["task_id"]])) + " out of balance" if bad_by_task.get(t["task_id"]) else ""}</p>'
        + timeline_svg(t["task_id"], events_by_task[t["task_id"]], bad_by_task.get(t["task_id"], set())) + "</div>"
        for t in drawn)
    more = len(order) - len(drawn)
    tl_more = f'<p class="mono">{more} more tasks not drawn; the per-task table above lists them all.</p>' if more > 0 else ""

    if segments:
        try:
            summary = verify_series(ledger.path)["segments"]
            series = "<ul>" + "".join(
                f"<li class=\"mono\">{_e(s['file'])}: {s['lines']} lines, seq {_e(s['first_seq'])} to "
                f"{_e(s['last_seq'])}, head {_e(s['head'][:16])}</li>" for s in summary) + "</ul>"
        except ChainError as exc:
            series = f"<p>The segments could not be re-read: {_e(exc)}</p>"
    else:
        series = "<p>One file, never rotated.</p>"

    cols = "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in COLUMN_NOTES.items())
    chain_text = "CHAIN VERIFIED" if chain_ok else "CHAIN BROKEN ON RE-READ"

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,500&family=Public+Sans:wght@400;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body><div class="frame">
<header><p class="brand">Dinand Tinholt · Research</p>
<p class="mono">DELEGATION LEDGER · AS OF {_e(bal.as_of)} · {len(entries)} ENTRIES · {len(segments) + 1} FILE(S) · HEAD {_e(ledger.head[:16])} · {chain_text}</p>
<h1>{_e(title)}</h1>
<div class="tally">{tally}</div></header>
<h2>Imbalances</h2>{imbal}
<h2>Trial balance</h2>
<div class="wrap"><table><caption>One line per task, agent and step</caption><thead><tr><th scope="col">task</th><th scope="col">agent</th><th scope="col">step</th><th scope="col" class="n">owed</th><th scope="col" class="n">ran</th><th scope="col" class="n">owed amount</th><th scope="col" class="n">ran amount</th><th scope="col">status</th><th scope="col">reason</th><th scope="col">due</th></tr></thead>
<tbody>{''.join(trs)}</tbody></table></div>
<h2>By agent</h2>
<div class="wrap"><table><caption>Lines per agent by status</caption><thead><tr><th scope="col">agent</th><th scope="col" class="n">tasks</th><th scope="col" class="n">lines</th><th scope="col" class="n">owed</th><th scope="col" class="n">ran</th>{''.join(f'<th scope="col" class="n">{s}</th>' for s in STATUSES)}</tr></thead>
<tbody>{agent_rows}</tbody></table></div>
<h2>By task</h2>
<div class="wrap"><table><caption>Lines per task by status</caption><thead><tr><th scope="col">task</th><th scope="col">agents</th><th scope="col" class="n">lines</th>{''.join(f'<th scope="col" class="n">{s}</th>' for s in STATUSES)}<th scope="col">closed</th><th scope="col">first line</th><th scope="col">last line</th></tr></thead>
<tbody>{task_rows}</tbody></table></div>
<h2>Timeline per task</h2>
<p class="legend">square above the rule: owed · circle below: ran · bar across: task closed · a step out of balance: heavy diamond above, large ringed circle below, double-ruled stamp in the tables · hover a mark for its line</p>
{tls}{tl_more}
<h2>Segments</h2>{series}
<h2>What each column means</h2><dl class="cols">{cols}</dl>
<h2>What this report cannot see</h2>
<p class="note">{_e(BLIND_SPOT)}</p>
<p class="mono">source: Dinand Tinholt, Research, "A Trial Balance for Agent Omissions", campaign page, published 2026-10-01, <a href="https://dinand.com/research/campaigns/trial-balance/">dinand.com/research/campaigns/trial-balance</a></p>
<p class="mono"><a href="https://dinand.com/research/">dinand.com/research</a></p>
</div></body></html>
"""


CSV_COLUMNS = COLUMNS + ["reason", "due"]
_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def _cell(value, column):
    s = "" if value is None else str(value)
    if column in ("task_id", "agent", "step") and s.startswith(_FORMULA):
        return "'" + s  # a spreadsheet would read these as a formula
    return s


def write_csv(ledger, fh, as_of=None) -> int:
    """Write the trial balance as CSV to the open text file ``fh``. Task, agent
    and step cells that start with = + - @ get a leading apostrophe so a
    spreadsheet shows them as text. Returns the number of data rows."""
    rows = ledger.trial_balance(as_of)
    w = csv.writer(fh, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for r in rows:
        w.writerow([_cell(r[c], c) for c in CSV_COLUMNS])
    return len(rows)
