# Changelog

## 0.2.1, 10 October 2026: fifteen fixes from the second independent check

Code defects, each with a test (named in brackets):

1. `owe` and `ran` accepted `None` for agent or step and wrote a line the loader then refused. Both now require them; only `close` carries none. [`test_1_owe_and_ran_need_agent_and_step`]
2. Page `?ledger=`: values are trimmed and parsed as URLs against the page, and anything off this origin is refused with a visible warning: leading space or tab, `//`, backslashes, `data:`, `javascript:`, credentials. From disk nothing is fetched. [page check "?ledger= takes only same-origin paths"; five smoke assertions]
3. Page verification now goes line by line in Python's order. Hashes are still computed in parallel batches first. An amount tamper on line 3 plus a deletion of line 6 is reported at line 3 ("hash does not match"), and the display keeps lines 1 and 2. [two page checks]
4. Timeline min and max used `Math.min(...times)`, which overflows the stack on large tasks; it is now a loop. [page check: 140,000 lines in one task]
5. A due or as-of time whose UTC value leaves years 1 to 9999 raised a raw `OverflowError`; it is now `LedgerError` naming the value, and the CLI exits 3. [`test_5_time_that_overflows_in_utc`]
6. A failed append (full disk, file-size limit, fsync error) could leave a partial line. The file is now cut back to its last good byte before the error propagates. A failed rotation removes its `.tmp`. [`test_6_failed_write_leaves_no_partial_line`, `test_6_file_size_limit_mid_line` (RLIMIT_FSIZE in a child process), `test_6_failed_carry_write_removes_tmp`]
7. Seal `segment` and carry `from` must be bare names of the form `stem.NNNNNN.ext`. Separators, `..`, absolute paths and control characters are refused by the loader, by the page, and again before the rename. [`test_7_seal_segment_must_be_a_bare_segment_name`, page check]
8. After the last timeline batch, focus moves to the last task drawn.
9. Picked files are read in pick order. A newer pick takes the token before any `f.text()`, so it always wins over an older pick.
10. Report and page timelines: an owed step out of balance is a heavy-stroked diamond, and a run out of balance is a larger ringed circle. Neither relies on colour. [`test_timeline`]
11. Report tables have captions, `scope="col"` headers and `scope="row"` first cells. [`test_report_tables_have_captions_and_scope`]
12. A requested ledger that fails to fetch, or is refused, shows a visible warning. There is no fallback to the sample. [smoke]
13. Amount rule kept and documented in the README: an obligation with no amount accepts any run amount, and one with an amount requires an equal one. [`test_13_amount_rules`]
14. The README notes what `rotate()` does when the ledger path is a symbolic link, verified by running it. [`test_rotating_through_a_symlink`]
15. Copy and render changes:
    - Oxide red now appears once on the page, on the blind-spot stamp; the blind-spot rule is ink.
    - "By design" appears once across the documents.
    - "Usually" and "most likely" are gone.
    - The two "landing" sentences and the two lists I could identify in the README and on the page are rewritten. The check's exact quotes were not in the message I received.
    - At 390 px, "What the generator injected" reads as blocks, so nothing scrolls sideways.
    - In print, each column heading stays with its definition, and the campaign figures keep their size.

The self-check also stopped disturbing the page. A check that loads a tampered ledger now restores whatever was on screen, along with the recorded timings. Smoke greps read only the status element: the page's script text contains "chain verified", so the earlier greps could have matched it.

Counts: Python tests 102 to 111, page checks 22 to 27, smoke assertions 12 to 17.

## 0.2.0, 10 October 2026: stress, review, elevate

### What broke under stress (found with hostile input against 0.1.0)

| input | 0.1.0 | 0.2.0 |
|---|---|---|
| U+2028, U+2029, NEL or U+001C to U+001E inside a name | written fine, then the file could not be read back: `splitlines()` split the line in the middle | files are split on newline only, in Python and in the page |
| a lone surrogate in a name | `UnicodeEncodeError` escaped mid-write | refused with `LedgerError` before anything is written |
| a due time with an offset, such as `-06:00` | stored with the offset; the earliest due was picked by string order, so 10:00-05:00 (15:00 UTC) beat 14:00Z | stored in UTC; the earliest due is picked by instant |
| `due="tomorrow"`, `--as-of later` | raw `ValueError` traceback from the CLI | `LedgerError`, CLI exit 3 with a message |
| amount `1e3` | stored as `1E+3`, which the page's decimal comparison did not understand | stored as plain `1000`; at most 30 integer and 18 decimal digits |
| amount `1e999999` | stored | refused as out of range |
| invalid UTF-8 bytes in the file | raw `UnicodeDecodeError` | `ChainError` naming the line |
| a forged line with float `seq`, float amount, extra fields or nested values but a correct SHA-256 | loaded and balanced | refused: every field is type-checked before hashing, in Python and in the page |
| a forged amount holding an `<img onerror>` payload, hash valid | the 0.1.0 page source inserted amount cells into the table without escaping (read from the code; I did not run that version in a browser) | refused by the structure check; every view escapes every value anyway |
| a due time from the file in the imbalance list | inserted without escaping (read from the 0.1.0 source) | escaped |
| 5 MB task id | accepted | refused above 512 characters |
| 3,000 appends through one file | 20.75 s, because every append re-read and re-hashed the whole file | 0.22 s; 12,200 appends take 1.07 s. Appends read only the bytes added since the last read |
| a reader opening the file while another process rotated it (found by the new concurrency test, 12 failures in 80 runs) | n/a in 0.1.0 | readers take a shared lock; 0 failures in 80 runs |
| a stray empty `ledger.000001.jsonl` next to the ledger | n/a | `rotate()` re-lists the segments under the lock and refuses, rather than sealing into a broken series |
| `render()` on a series whose sealed segment was tampered with after loading | n/a | renders, says "CHAIN BROKEN ON RE-READ" and names the break |

Inputs that held in both versions: empty file, duplicate ids (counted, as designed), duplicate lines (chain catches the second), negative amounts, NaN and infinity, deep nesting (refused as not an object), CRLF line endings. A leading BOM is now tolerated.

### Hostile review of my own code

- `commission()` wrote obligations one at a time, so a bad fifth step left four written. It now checks the whole plan first.
- `observed()` documents that a failed ledger write after a successful tool call propagates. Hiding it would lose a run.
- Names with ANSI escape sequences could reach the terminal through `balance`, `watch` and error messages. CLI output now shows control characters as `\x1b` and so on.
- CSV export guards task, agent and step cells that start with `= + - @` against spreadsheet formula injection. Amount cells stay numbers.
- Python's `str.strip()` treats U+001C to U+001F as whitespace and JavaScript's `trim()` does not, so the two sides disagreed on an empty name. Both now use the same six ASCII whitespace characters.
- Page: a silent `catch` around the sample fetch now logs a warning. Drag and drop outside the drop zone no longer navigates away from the page. A newer load cancels an older one mid-check, so a slow file cannot overwrite a fast one. "Show all" buttons keep focus. Every table has a caption and column scopes. A skip link was added. Bad statuses carry a double-ruled border as well as the accent colour, and timeline marks differ by shape. On a broken chain the page shows only the lines that verified before the break.
- Page layout: imbalance rows overflowed at 390 px (grid min-width). In print, task ids broke mid-token. Both fixed.

### Added

- `Ledger.rotate()`: seal line, numbered segment, carry line holding the sealed head; `verify()`, `balance()` and the CLI read the whole series. A crash between any two rotation steps is finished by the next append or rotation. `verify_series()` reports every segment. CLI `rotate`.
- Locking on `ledger.jsonl.lock`: exclusive for appends and rotation, shared for reads.
- `report --html`: views per agent and per task, an SVG timeline of owe, ran and close lines per task, the segment list, a "what each column means" panel, the blind-spot note.
- `export --csv OUT|-`.
- `watch [--interval S] [--count N]`: polls, prints new imbalances once, notes cleared ones, reports a chain break once.
- `examples/agent_loop.py`: a generic agent loop wired to both handles with a scripted policy, so it runs without a model.
- Demo page: drop or pick one file or several segments, paste up to 64 MB, `?ledger=` for same-site files, the agent, task and timeline views, rotation status, the column panel, and a progress note while the chain is checked.

### Test and check counts

| | before | after |
|---|---|---|
| Python tests (`-W error`) | 28 | 102 |
| page console self-checks | 2 (plus one assert) | 22 |
| headless smoke assertions | none | 12 (`tests/smoke.sh`) |
