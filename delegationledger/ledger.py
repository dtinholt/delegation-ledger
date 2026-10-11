"""The Delegation Ledger: two books for agent work.

The obligation book holds what an agent owes, written by whoever hands out the
work, before the agent acts. The resource book holds what actually ran, written
by whoever observes the tool calls. The agent writes neither. The balance reads
only where the two books disagree.

Both books live in one append-only JSONL file. Every line carries the SHA-256 of
the line before it, so an edit or a deletion anywhere in the file breaks the
chain from that point on, and ``verify_chain`` names the line. A partial last
line from an interrupted write stops every read and append until you repair it
by hand.

``Ledger.rotate()`` seals the live file with a final line, moves it aside as a
numbered segment and starts a new live file whose first line carries the sealed
head. The chain runs on across segments, and the balance reads the whole series.

What the balance can and cannot see is fixed by construction:

* an owed step with no run behind it is visible: open while it can still run,
  skipped once it cannot (see ``Ledger.balance``);
* a run with no obligation behind it is visible (unowed);
* an amount that differs between the two sides is visible;
* a task the commissioner closes turns every step still owed into a skipped
  step from that moment; a task with no close and no due times stays open, by
  design, because the library never invents a deadline;
* an amount that is wrong on both sides is invisible. If the wrong value entered
  before the work was assigned, both books carry it and the line balances. The
  Trial Balance campaign's balance check found 0 of 1,044 of these.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

try:  # POSIX advisory lock; absent on Windows, where appends are not locked
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

GENESIS = "0" * 64
FORMAT = "delegationledger/1"
COMMISSIONER = "commissioner"
HARNESS = "harness"
ROLES = (COMMISSIONER, HARNESS)
BOOK_ROLE = {"owe": COMMISSIONER, "close": COMMISSIONER, "ran": HARNESS,
             "seal": COMMISSIONER, "carry": COMMISSIONER}
WORK_BOOKS = ("owe", "ran", "close")

_COMMON = {"fmt", "book", "role", "by", "ts", "seq", "prev", "hash"}
KEYS = {
    "owe": _COMMON | {"task_id", "agent", "step", "amount", "due"},
    "ran": _COMMON | {"task_id", "agent", "step", "amount", "due"},
    "close": _COMMON | {"task_id", "agent", "step", "amount", "due"},
    "seal": _COMMON | {"segment", "lines"},
    "carry": _COMMON | {"from", "head"},
}

MAX_NAME = 512          # characters in task_id, agent, step, by
MAX_INT_DIGITS = 30     # an amount's integer part
MAX_FRAC_DIGITS = 18    # an amount's fractional part
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_AMOUNT = re.compile(r"-?(0|[1-9][0-9]{0,%d})(\.[0-9]{1,%d})?\Z" % (MAX_INT_DIGITS - 1, MAX_FRAC_DIGITS))

BLIND_SPOT = (
    "A wrong amount carried on both sides balances. If the error entered before "
    "the work was assigned, the obligation and the run agree on the wrong value and "
    "this check reports nothing. In the Trial Balance campaign the balance check "
    "found 0 of 1,044 such faults; a full read of the log, where the source "
    "document still shows the true amount, found them almost every time."
)


class LedgerError(Exception):
    """Base class for ledger errors."""


class RoleError(LedgerError, AttributeError):
    """A handle was used for the other book's verb. It is an AttributeError so
    ``hasattr(harness, "owe")`` is False."""


class ChainError(LedgerError):
    """The hash chain does not verify. ``line`` is 1-based within ``file``
    (the file's base name, or None for entries held in memory)."""

    def __init__(self, line, reason, file=None):
        where = f"{file} line {line}" if file else f"line {line}"
        super().__init__(f"hash chain broken at {where}: {reason}")
        self.line = line
        self.reason = reason
        self.file = file


def canonical(obj) -> str:
    """The exact bytes that get hashed. The JavaScript view reproduces this."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entry_hash(body: dict) -> str:
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


def norm_amount(amount):
    """Amounts are stored as plain decimal strings (no exponent) so the hash
    never depends on float formatting and the browser view can compare them
    without floats. ``None`` stays ``None``. Up to 30 integer digits and 18
    fractional digits; anything else raises ``LedgerError``."""
    if amount is None:
        return None
    if isinstance(amount, bool):
        raise LedgerError("amount must be a number, got a bool")
    try:
        d = Decimal(str(amount).strip())
    except (InvalidOperation, ValueError):
        raise LedgerError(f"amount is not a number: {str(amount)[:40]!r}")
    if not d.is_finite():
        raise LedgerError(f"amount must be finite: {amount!r}")
    if d.adjusted() >= MAX_INT_DIGITS:
        raise LedgerError(f"amount out of range: more than {MAX_INT_DIGITS} integer digits")
    if d == 0:
        d = abs(d)
    exp = d.as_tuple().exponent
    if exp < -MAX_FRAC_DIGITS:
        raise LedgerError(f"amount out of range: more than {MAX_FRAC_DIGITS} decimal places")
    if exp > 0:
        d = d.quantize(Decimal(1))
    s = format(d, "f")
    if not _AMOUNT.match(s):  # pragma: no cover - guarded by the checks above
        raise LedgerError(f"amount out of range: {s[:40]!r}")
    return s


def same_amount(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return Decimal(a) == Decimal(b)


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_time(s, label="time"):
    """ISO 8601 to an aware datetime; no offset means UTC. Raises LedgerError."""
    if s is None:
        return None
    if isinstance(s, datetime):
        dt = s
    else:
        txt = str(s).strip()
        if txt.endswith(("Z", "z")):
            txt = txt[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(txt)
        except ValueError:
            raise LedgerError(f"{label} is not an ISO 8601 time: {str(s)[:40]!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _utc_text(dt) -> str:
    """An aware datetime as ISO 8601 UTC. A time whose UTC equivalent falls
    outside years 1 to 9999 raises LedgerError naming the value."""
    try:
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, ValueError):
        raise LedgerError(f"time out of range once converted to UTC: {dt.isoformat()!r}")


_SEGMENT = re.compile(r"[^/\\\x00-\x1f]+\.[0-9]{6}(\.[^./\\\x00-\x1f]+)?\Z")


def _segment_ok(name) -> bool:
    """A rotation segment name: a bare base name ``stem.NNNNNN[.ext]``, no
    directory separator, no ``..``, nothing absolute."""
    return (isinstance(name, str) and len(name) <= MAX_NAME and bool(_SEGMENT.match(name))
            and not name.startswith(".") and ".." not in name)


_ASCII_SPACE = " \t\n\r\x0b\x0c"  # the browser view uses the same set, so both agree on "empty"


def _name_problem(value, max_len=MAX_NAME):
    if not isinstance(value, str) or not value.strip(_ASCII_SPACE):
        return "must be a non-empty string"
    if len(value) > max_len:
        return f"is longer than {max_len} characters"
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return "is not valid Unicode (lone surrogate)"
    return None


def _check_name(label, value):
    problem = _name_problem(value)
    if problem:
        raise LedgerError(f"{label} {problem}")
    return value


@dataclass
class Row:
    """One line of the trial balance: a (task, agent, step) and both sides."""
    task_id: str
    agent: str
    step: str
    owed: int
    ran: int
    owed_amounts: list
    ran_amounts: list
    status: str  # ok | open | skipped | amount | unowed
    due: str | None = None
    reason: str | None = None  # for skipped: moved_on | overdue | closed


@dataclass
class Balance:
    """What ``Ledger.balance()`` returns. Empty lists mean the two books agree
    with each other; a value wrong in both of them passes (see ``BLIND_SPOT``)."""
    as_of: str
    open_obligations: list = field(default_factory=list)
    skipped_steps: list = field(default_factory=list)
    amount_mismatches: list = field(default_factory=list)
    unowed_runs: list = field(default_factory=list)
    blind_spot: str = BLIND_SPOT

    @property
    def balanced(self) -> bool:
        return not (self.skipped_steps or self.amount_mismatches or self.unowed_runs or self.open_obligations)

    @property
    def imbalances(self) -> list:
        """Everything that is wrong now. Open obligations are not wrong yet."""
        return self.skipped_steps + self.amount_mismatches + self.unowed_runs

    def to_dict(self):
        d = asdict(self)
        d["balanced"] = self.balanced
        return d


# -- the series on disk ----------------------------------------------------------
def _split_path(path):
    d, base = os.path.split(os.fspath(path))
    stem, ext = os.path.splitext(base)
    return d or ".", stem, ext


def segment_name(path, n) -> str:
    """Base name of archived segment ``n`` of the ledger at ``path``:
    ``ledger.jsonl`` -> ``ledger.000001.jsonl``."""
    _, stem, ext = _split_path(path)
    return f"{stem}.{n:06d}{ext}"


def segment_paths(path) -> list:
    """Archived segments of the ledger at ``path``, oldest first, as
    ``(number, full_path)``."""
    d, stem, ext = _split_path(path)
    rx = re.compile(re.escape(stem) + r"\.(\d{6})" + re.escape(ext) + r"\Z")
    try:
        names = os.listdir(d)
    except FileNotFoundError:
        return []
    found = []
    for name in names:
        m = rx.match(name)
        if m:
            found.append((int(m.group(1)), os.path.join(d, name)))
    return sorted(found)


@contextlib.contextmanager
def _series_lock(path, exclusive=True):
    """flock on ``path + ".lock"``. No-op where fcntl is missing. A reader in a
    directory it cannot write reads without the shared lock."""
    if fcntl is None:
        yield
        return
    try:
        fh = open(os.fspath(path) + ".lock", "a")
    except OSError:
        if exclusive:
            raise
        yield
        return
    try:
        fcntl.flock(fh, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


class Ledger:
    """An obligation book and a resource book in one hash-chained JSONL file.

    ``Ledger()`` keeps entries in memory. ``Ledger(path)`` loads and verifies the
    file, and any segments ``rotate()`` moved aside, and appends to it, one fsync
    per line. Appends and rotation take an exclusive lock on ``path + ".lock"``
    and read only what other writers added since the last read.

    A ``Ledger`` can write both books, which suits tests and single-process
    scripts. In a deployment use ``Ledger.open(path, "commissioner")`` in the
    dispatcher and ``Ledger.open(path, "harness")`` where tools execute: each
    handle can write only its own book.
    """

    @classmethod
    def open(cls, path, role, clock=None):
        """A handle that can write only one book. ``role`` is "commissioner"
        (``owe``, ``close``, ``rotate``) or "harness" (``ran``). Both can read."""
        if role == COMMISSIONER:
            return CommissionerHandle(cls(path, clock=clock))
        if role == HARNESS:
            return HarnessHandle(cls(path, clock=clock))
        raise RoleError(f"role must be one of {ROLES}, got {role!r}")

    def __init__(self, path=None, clock=None):
        self.path = os.fspath(path) if path is not None else None
        self._clock = clock or _now
        self.entries: list[dict] = []
        self._arch_cache = {}      # segment path -> ((size, mtime_ns), entries)
        self._arch_names = []      # base names of archived segments in the series
        self._live_start = 0       # index in entries where the live file begins
        self._live_size = 0        # bytes of the live file already read
        self._live_tail = b""      # its last line, to detect a rewrite or a rotation
        self._held = False         # this object holds the series lock
        if self.path:
            self._sync()

    # -- reading the file ------------------------------------------------------
    @property
    def head(self) -> str:
        return self.entries[-1]["hash"] if self.entries else GENESIS

    @property
    def segments(self) -> list:
        """Base names of the archived segments, oldest first. The live file is
        not included."""
        return list(self._arch_names)

    @contextlib.contextmanager
    def _lock(self, exclusive=True):
        """Exclusive for writers, shared for readers, so a reader never sees a
        rotation half done. Re-entrant within one Ledger: a writer that holds
        the exclusive lock reads under it without locking again."""
        if self._held or not self.path:
            yield
            return
        with _series_lock(self.path, exclusive):
            self._held = True
            try:
                yield
            finally:
                self._held = False

    def _refresh(self):
        """Bring ``entries`` up to date with the file. Kept for callers of 0.1."""
        self._sync()

    def _sync(self, full=False):
        if not self.path:
            return
        with self._lock(exclusive=False):
            self._sync_locked(full)

    def _sync_locked(self, full):
        if not full and self._live_size:
            try:
                with open(self.path, "rb") as fh:
                    fh.seek(self._live_size - len(self._live_tail))
                    chunk = fh.read()
            except FileNotFoundError:
                chunk = b""
            if chunk.startswith(self._live_tail):
                new = chunk[len(self._live_tail):]
                if new:
                    first = len(self.entries) - self._live_start + 1
                    fresh = _parse_bytes(new, first_line=first, file=os.path.basename(self.path))
                    verify_chain(fresh, prev=self.head, seq=len(self.entries) + 1, first_line=first,
                                 file=os.path.basename(self.path), after=self.entries[-1]["book"])
                    self.entries.extend(fresh)
                    self._live_size += len(new)
                    self._live_tail = new[new.rstrip(b"\n").rfind(b"\n") + 1:]
                return
        self._reload(use_cache=not full)

    def _reload(self, use_cache=True):
        arch, names, live, raw = _read_series(self.path, self._arch_cache if use_cache else {})
        self._arch_cache = arch
        self._arch_names = names
        prefix = [e for _, es in arch.values() for e in es]
        self.entries = prefix + live
        self._live_start = len(prefix)
        self._live_size = len(raw)
        body = raw.rstrip(b"\n")
        self._live_tail = raw[body.rfind(b"\n") + 1:] if raw else b""

    def verify(self) -> bool:
        """Re-read every file of the series from disk (or check the entries in
        memory) and verify the whole chain. Returns False on any break;
        ``verify_series`` raises with the file and line if you need them."""
        try:
            if self.path:
                with self._lock(exclusive=False):
                    _read_series(self.path, {})
            else:
                verify_chain(self.entries)
        except ChainError:
            return False
        return True

    # -- writing ---------------------------------------------------------------
    def _line(self, body):
        body["seq"] = len(self.entries) + 1
        body["prev"] = self.head
        entry = dict(body, hash=entry_hash(body))
        return entry, (canonical(entry) + "\n").encode("utf-8")

    def _write(self, body):
        """Append one line. Call with the lock held and the entries in sync."""
        entry, data = self._line(body)
        if self.path:
            good = None
            try:
                with open(self.path, "ab") as fh:
                    good = fh.seek(0, os.SEEK_END)
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
            except BaseException:
                # leave no partial line behind: cut the file back to its last good byte
                if good is not None:
                    try:
                        os.truncate(self.path, good)
                    except OSError:  # pragma: no cover - the original error is the one to see
                        pass
                raise
            self._live_size += len(data)
            self._live_tail = data
        self.entries.append(entry)
        return entry

    def _append(self, body):
        if not self.path:
            return self._write(body)
        with self._lock():
            self._sync()
            self._heal()
            return self._write(body)

    def _heal(self):
        """Finish a rotation that a crash interrupted. Lock held, entries in sync."""
        live = self.entries[self._live_start:]
        if live and live[-1]["book"] == "seal":
            self._finish_rotation()
        elif not live and self.entries and self.entries[-1]["book"] == "seal":
            self._write_carry()

    def _finish_rotation(self):
        seal = self.entries[-1]
        if not _segment_ok(seal["segment"]):
            raise LedgerError(f"cannot rotate: {str(seal['segment'])[:60]!r} is not a segment file name")
        d, _, _ = _split_path(self.path)
        target = os.path.join(d, seal["segment"])
        if os.path.exists(target):
            raise LedgerError(f"cannot rotate: {seal['segment']} already exists")
        os.rename(self.path, target)
        _fsync_dir(d)
        st = os.stat(target)
        live = self.entries[self._live_start:]
        self._arch_cache[target] = ((st.st_size, st.st_mtime_ns), live)
        self._arch_names.append(seal["segment"])
        self._live_start = len(self.entries)
        self._live_size, self._live_tail = 0, b""
        self._write_carry()

    def _write_carry(self):
        seal = self.entries[-1]
        entry, data = self._line({"fmt": FORMAT, "book": "carry", "role": COMMISSIONER,
                                  "from": seal["segment"], "head": seal["hash"],
                                  "by": seal["by"], "ts": self._clock()})
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(tmp)
            raise  # readers see no live file or the whole carry line
        _fsync_dir(_split_path(self.path)[0])
        self._live_size, self._live_tail = len(data), data
        self.entries.append(entry)
        return entry

    def _work(self, book, role, task_id, agent, step, amount, due, by):
        body = {"fmt": FORMAT, "book": book, "role": role,
                "task_id": _check_name("task_id", task_id),
                # only a close line carries no agent or step; owe and ran must name both,
                # so a writer can never produce a line the loader would refuse
                "agent": None if book == "close" else _check_name("agent", agent),
                "step": None if book == "close" else _check_name("step", step),
                "amount": norm_amount(amount),
                "due": _utc_text(_parse_time(due, "due")) if due is not None else None,
                "by": _check_name("by", by), "ts": self._clock()}
        if not isinstance(body["ts"], str):
            raise LedgerError("the clock must return a string")
        return self._append(body)

    def owe(self, task_id, agent, step, amount=None, due=None, *, by="commissioning"):
        """Record that ``agent`` owes ``step`` on ``task_id``. Call this when the
        work is assigned, before the agent acts. ``due`` is stored in UTC."""
        return self._work("owe", COMMISSIONER, task_id, agent, step, amount, due, by)

    def ran(self, task_id, agent, step, amount=None, *, by="harness"):
        """Record that ``step`` actually ran for ``agent`` on ``task_id``. Call
        this from the layer that executed the tool call, never from the agent."""
        return self._work("ran", HARNESS, task_id, agent, step, amount, None, by)

    def close(self, task_id, *, by="commissioning"):
        """Close a task. Every step still owed on it counts as skipped from this
        line on. Steps owed after the close are open again until closed."""
        return self._work("close", COMMISSIONER, task_id, None, None, None, None, by)

    def rotate(self, *, by="commissioning") -> dict:
        """Seal the live file and start a new one.

        Writes a ``seal`` line naming the segment the file becomes, renames the
        file to ``<stem>.NNNNNN<ext>``, and writes a new live file whose first
        line is a ``carry`` holding the seal's hash. Returns ``{"segment",
        "head", "carry"}`` where ``head`` is the sealed file's final hash. The
        balance and ``verify()`` read across every segment. A crash part way
        through is finished by the next append or rotation."""
        if not self.path:
            raise LedgerError("an in-memory ledger has no file to rotate")
        _check_name("by", by)
        with self._lock():
            self._reload()  # re-list the segments: a stray or broken one must stop the rotation
            self._heal()
            if len(self.entries) == self._live_start:
                raise LedgerError("nothing to rotate: the live file is empty")
            numbers = [n for n, _ in segment_paths(self.path)]
            name = segment_name(self.path, (max(numbers) if numbers else 0) + 1)
            lines = len(self.entries) - self._live_start + 1
            seal = self._write({"fmt": FORMAT, "book": "seal", "role": COMMISSIONER,
                                "segment": name, "lines": lines, "by": by, "ts": self._clock()})
            self._finish_rotation()
            return {"segment": name, "head": seal["hash"], "carry": self.entries[-1]}

    # -- the balance -----------------------------------------------------------
    def rows(self, as_of=None) -> list[Row]:
        self._sync()
        as_of_dt = _parse_time(as_of, "as_of") if as_of is not None else _parse_time(_now())
        owed = defaultdict(list)    # key -> list of owe entries
        ran = defaultdict(list)
        order = defaultdict(list)   # (task, agent) -> steps in the order they were owed
        first_seen = {}
        closed_at = {}              # task -> seq of its latest close line
        for e in self.entries:
            book = e["book"]
            if book == "close":
                closed_at[e["task_id"]] = e["seq"]
                continue
            if book not in ("owe", "ran"):
                continue
            key = (e["task_id"], e["agent"], e["step"])
            first_seen.setdefault(key, e["seq"])
            if book == "owe":
                if key not in owed:
                    order[key[:2]].append(key[2])
                owed[key].append(e)
            else:
                ran[key].append(e)

        position = {ta: {s: i for i, s in enumerate(steps)} for ta, steps in order.items()}
        rows = []
        for key in sorted(set(owed) | set(ran), key=lambda k: (k[0], k[1], first_seen[k])):
            o, r = owed.get(key, []), ran.get(key, [])
            oa = [x["amount"] for x in o]
            ra = [x["amount"] for x in r]
            dues = [(_parse_time(x["due"]), x["due"]) for x in o if x["due"]]
            due_dt, due = min(dues) if dues else (None, None)
            reason = None
            if len(r) > len(o):
                status = "unowed"
            elif len(r) < len(o):
                seq = order[key[:2]]
                i = position[key[:2]].get(key[2])
                later = seq[i + 1:] if i is not None else []
                moved_on = any(ran.get((key[0], key[1], s)) for s in later)
                overdue = due_dt is not None and due_dt < as_of_dt
                closed = closed_at.get(key[0], 0) > max(x["seq"] for x in o)
                reason = "closed" if closed else "moved_on" if moved_on else "overdue" if overdue else None
                status = "skipped" if reason else "open"
            elif any(a is not None and not same_amount(a, b) for a, b in zip(oa, ra)):
                status = "amount"
            else:
                status = "ok"
            rows.append(Row(key[0], key[1], key[2], len(o), len(r), oa, ra, status, due, reason))
        return rows

    def balance(self, as_of=None) -> Balance:
        """Open obligations, skipped steps, amount mismatches and unowed runs.

        A step is *skipped* when it was owed, did not run, and one of three
        things holds: the commissioner closed the task after owing it, a step
        owed after it on the same task and agent has run, or its due time is
        before ``as_of``. Otherwise a missing run is *open*, and stays open:
        there is no default deadline. ``as_of`` defaults to now (UTC).
        """
        as_of_s = _utc_text(_parse_time(as_of, "as_of") if as_of is not None else _parse_time(_now()))
        b = Balance(as_of=as_of_s)
        for row in self.rows(as_of_s):
            item = {"task_id": row.task_id, "agent": row.agent, "step": row.step,
                    "owed": row.owed, "ran": row.ran}
            if row.status == "open":
                b.open_obligations.append(dict(item, due=row.due))
            elif row.status == "skipped":
                b.skipped_steps.append(dict(item, due=row.due, reason=row.reason))
            elif row.status == "amount":
                b.amount_mismatches.append(dict(item, owed_amounts=row.owed_amounts, ran_amounts=row.ran_amounts))
            elif row.status == "unowed":
                b.unowed_runs.append(item)
        return b

    def trial_balance(self, as_of=None) -> list[dict]:
        """Every (task, agent, step) with both sides and a status, as a list of
        dicts. ``format_table`` turns it into text."""
        out = []
        for r in self.rows(as_of):
            out.append({
                "task_id": r.task_id, "agent": r.agent, "step": r.step,
                "owed": r.owed, "ran": r.ran,
                "owed_amount": _amt_cell(r.owed_amounts), "ran_amount": _amt_cell(r.ran_amounts),
                "status": r.status, "reason": r.reason or "", "due": r.due or "",
            })
        return out


def _amt_cell(amounts):
    vals = [a for a in amounts if a is not None]
    return ", ".join(vals) if vals else ""


COLUMNS = ["task_id", "agent", "step", "owed", "ran", "owed_amount", "ran_amount", "status"]
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


def printable(s) -> str:
    """Text safe for a terminal: control characters, which could carry escape
    sequences from a hostile name, become ``\\xNN`` or ``\\uNNNN``."""
    return _CONTROL.sub(lambda m: "\\x%02x" % ord(m.group()) if ord(m.group()) < 256
                        else "\\u%04x" % ord(m.group()), str(s))


def format_table(rows) -> str:
    head = {c: c.replace("_", " ") for c in COLUMNS}
    cells = [head] + [{c: printable(r[c]) for c in COLUMNS} for r in rows]
    width = {c: max(len(x[c]) for x in cells) for c in COLUMNS}
    lines = []
    for i, x in enumerate(cells):
        lines.append("  ".join(x[c].rjust(width[c]) if c in ("owed", "ran", "owed_amount", "ran_amount")
                               else x[c].ljust(width[c]) for c in COLUMNS).rstrip())
        if i == 0:
            lines.append("  ".join("-" * width[c] for c in COLUMNS))
    return "\n".join(lines)


# -- persistence -----------------------------------------------------------------
PARTIAL_REPAIR = ("the file does not end with a newline, so its last line is partial, as an "
                  "interrupted write leaves it; inspect that line, then truncate the partial tail by hand")


def _fsync_dir(d):
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:  # pragma: no cover - platforms without directory handles
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)


def _parse_bytes(data, first_line=1, file=None):
    """Bytes of whole lines to a list of dicts, structure-checked but with the
    chain unchecked. Splits on newline only: a name may hold U+2028 or NEL."""
    if first_line == 1 and data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    if data and not data.endswith(b"\n"):
        raise ChainError(first_line + data.count(b"\n"), PARTIAL_REPAIR, file)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ChainError(first_line + data[:exc.start].count(b"\n"), "not valid UTF-8", file)
    lines = text.split("\n")[:-1] if text else []
    out = []
    for n, line in enumerate(lines, first_line):
        line = line[:-1] if line.endswith("\r") else line
        if not line.strip():
            raise ChainError(n, "blank line", file)
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ChainError(n, f"not JSON ({exc.msg})", file)
        except (ValueError, RecursionError):
            raise ChainError(n, "not JSON (nested too deeply or malformed)", file)
        if not isinstance(obj, dict):
            raise ChainError(n, "not a JSON object", file)
        out.append(obj)
    return out


def _start_prev(entries):
    """A segment read on its own starts at its carry line's head."""
    if entries and entries[0].get("book") == "carry" and isinstance(entries[0].get("prev"), str):
        return entries[0]["prev"]
    return GENESIS


def _start_seq(entries):
    if entries and entries[0].get("book") == "carry" and type(entries[0].get("seq")) is int:
        return entries[0]["seq"]
    return 1


def load(path) -> list[dict]:
    """Read and verify one file on its own. A segment that starts with a carry
    line is checked from that line's head; ``verify_series`` checks the links."""
    with open(path, "rb") as fh:
        data = fh.read()
    name = os.path.basename(os.fspath(path))
    entries = _parse_bytes(data, file=name)
    verify_chain(entries, prev=_start_prev(entries), seq=_start_seq(entries), file=name)
    return entries


def _read_series(path, cache):
    """Read every archived segment and the live file, verifying each and the
    links between them. Unchanged segments come from ``cache``."""
    new_cache, names, entries = {}, [], []
    prev, seq, prev_name = GENESIS, 1, None
    for _, p in segment_paths(path):
        name = os.path.basename(p)
        st = os.stat(p)
        stamp = (st.st_size, st.st_mtime_ns)
        hit = cache.get(p)
        if hit and hit[0] == stamp and hit[1] and hit[1][0].get("prev") == prev and hit[1][0].get("seq") == seq:
            es = hit[1]
        else:
            with open(p, "rb") as fh:
                es = _parse_bytes(fh.read(), file=name)
            if not es:
                raise ChainError(1, "archived segment is empty", name)
            verify_chain(es, prev=prev, seq=seq, file=name, carry_from=prev_name)
        if es[-1].get("book") != "seal" or es[-1].get("segment") != name:
            raise ChainError(len(es), "an archived segment must end with a seal line naming it", name)
        new_cache[p] = (stamp, es)
        names.append(name)
        entries.extend(es)
        prev, seq, prev_name = es[-1]["hash"], seq + len(es), name
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        raw = b""
    name = os.path.basename(path)
    live = _parse_bytes(raw, file=name)
    verify_chain(live, prev=prev, seq=seq, file=name, carry_from=prev_name)
    return new_cache, names, live, raw


def verify_series(path) -> dict:
    """Verify every segment of the ledger at ``path`` and the links between
    them. Raises ``ChainError`` naming the file and line. Returns a summary:
    ``{"segments": [{"file", "lines", "first_seq", "last_seq", "head"}], "entries", "head"}``
    where the live file is the last item."""
    with _series_lock(path, exclusive=False):
        cache, names, live, _ = _read_series(path, {})
    out, total, head = [], 0, GENESIS
    for p, (_, es) in sorted(cache.items()):
        out.append({"file": os.path.basename(p), "lines": len(es), "first_seq": es[0]["seq"],
                    "last_seq": es[-1]["seq"], "head": es[-1]["hash"]})
        total += len(es)
        head = es[-1]["hash"]
    if live:
        head = live[-1]["hash"]
    out.append({"file": os.path.basename(path), "lines": len(live),
                "first_seq": live[0]["seq"] if live else None,
                "last_seq": live[-1]["seq"] if live else None, "head": head})
    return {"segments": out, "entries": total + len(live), "head": head}


def _structure_problem(e):
    """What is wrong with a line's fields, before any hashing, or None."""
    book = e.get("book")
    if book not in KEYS:
        return f"unknown book {str(book)[:20]!r}"
    keys = set(e)
    if keys != KEYS[book]:
        missing, extra = sorted(KEYS[book] - keys), sorted(keys - KEYS[book])
        return "fields do not match a %s line (%s)" % (book, "; ".join(
            x for x in (f"missing {', '.join(missing)}" if missing else "",
                        f"unexpected {', '.join(k[:20] for k in extra[:3])}" if extra else "") if x))
    if e["fmt"] != FORMAT:
        return f"unknown format {str(e['fmt'])[:30]!r}"
    if e["role"] != BOOK_ROLE[book]:
        return f"a {book} line must carry role {BOOK_ROLE[book]!r}"
    if type(e["seq"]) is not int:
        return "seq is not an integer"
    for k in ("prev", "hash"):
        if not isinstance(e[k], str) or not _HEX64.match(e[k]):
            return f"{k} is not a SHA-256 hex digest"
    for k in ("by", "ts"):
        p = _name_problem(e[k])
        if p:
            return f"{k} {p}"
    if book in WORK_BOOKS:
        names = ("task_id", "agent", "step") if book != "close" else ("task_id",)
        for k in names:
            p = _name_problem(e[k])
            if p:
                return f"{k} {p}"
        if book == "close" and (e["agent"] is not None or e["step"] is not None or e["amount"] is not None):
            return "a close line carries only a task_id"
        a = e["amount"]
        if a is not None and not (isinstance(a, str) and _AMOUNT.match(a)):
            return "amount is not a plain decimal string"
        if e["due"] is not None:
            if book != "owe":
                return f"a {book} line carries no due time"
            if not isinstance(e["due"], str):
                return "due is not an ISO 8601 time"
            try:
                _parse_time(e["due"])
            except LedgerError:
                return "due is not an ISO 8601 time"
    elif book == "seal":
        if not _segment_ok(e["segment"]):
            return "segment is not a segment file name"
        if type(e["lines"]) is not int or e["lines"] < 1:
            return "lines is not a positive integer"
    else:  # carry
        if not _segment_ok(e["from"]):
            return "from is not a segment file name"
        if e["head"] != e["prev"]:
            return "a carry line's head must equal its prev"
    return None


def verify_chain(entries, prev=GENESIS, seq=1, first_line=1, file=None, carry_from=None, after=None):
    """Check structure, links and hashes. ``prev`` and ``seq`` give where the
    chain should pick up; ``carry_from``, when set, names the segment the first
    line must carry from. A seal must be the last line or be followed by its
    carry; a carry must be the first line or follow its seal. ``after`` is the
    book of the line before ``entries`` in the same file, when there is one."""
    for i, e in enumerate(entries):
        n = first_line + i
        if not isinstance(e, dict) or "hash" not in e:
            raise ChainError(n, "no hash field", file)
        problem = _structure_problem(e)
        if problem:
            raise ChainError(n, problem, file)
        if e["prev"] != prev:
            raise ChainError(n, "prev does not match the hash of the line before", file)
        if e["seq"] != seq + i:
            raise ChainError(n, f"seq is {e['seq']!r}, expected {seq + i}", file)
        book = e["book"]
        before = entries[i - 1]["book"] if i else after
        if book == "carry":
            if i == 0 and first_line == 1:
                if carry_from is not None and e["from"] != carry_from:
                    raise ChainError(n, f"carries from {e['from'][:40]!r}, expected {carry_from!r}", file)
            elif before != "seal":
                raise ChainError(n, "a carry line must open a file or follow a seal", file)
        elif i == 0 and first_line == 1 and carry_from is not None:
            raise ChainError(n, f"the file after {carry_from} must open with a carry line", file)
        if before == "seal" and book != "carry":
            raise ChainError(n, "lines after a seal", file)
        body = {k: v for k, v in e.items() if k != "hash"}
        if entry_hash(body) != e["hash"]:
            raise ChainError(n, "hash does not match the line's contents", file)
        prev = e["hash"]
    return True


# -- role handles ----------------------------------------------------------------
class _Handle:
    role = None

    def __init__(self, ledger):
        self._ledger = ledger

    def __getattr__(self, name):
        if name in ("owe", "ran", "close", "rotate"):
            raise RoleError(f"a {self.role} handle cannot {name}; "
                            f"open the ledger as {BOOK_ROLE['ran' if name == 'ran' else 'owe']!r} for that")
        raise AttributeError(name)

    @property
    def path(self):
        return self._ledger.path

    @property
    def entries(self):
        self._ledger._sync()
        return list(self._ledger.entries)

    @property
    def head(self):
        self._ledger._sync()
        return self._ledger.head

    @property
    def segments(self):
        self._ledger._sync()
        return self._ledger.segments

    def verify(self):
        return self._ledger.verify()

    def balance(self, as_of=None):
        return self._ledger.balance(as_of)

    def trial_balance(self, as_of=None):
        return self._ledger.trial_balance(as_of)


class CommissionerHandle(_Handle):
    """Lives in the dispatcher. Writes obligations, closes tasks, rotates the file."""
    role = COMMISSIONER

    def owe(self, task_id, agent, step, amount=None, due=None, *, by="commissioning"):
        return self._ledger.owe(task_id, agent, step, amount=amount, due=due, by=by)

    def close(self, task_id, *, by="commissioning"):
        return self._ledger.close(task_id, by=by)

    def rotate(self, *, by="commissioning"):
        return self._ledger.rotate(by=by)


class HarnessHandle(_Handle):
    """Lives where tools execute. Records what ran, nothing else."""
    role = HARNESS

    def ran(self, task_id, agent, step, amount=None, *, by="harness"):
        return self._ledger.ran(task_id, agent, step, amount=amount, by=by)
