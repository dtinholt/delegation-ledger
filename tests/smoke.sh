#!/bin/sh
# Headless smoke test for demo/index.html. Exits non-zero on any failure.
#
#   1. from disk: the console self-check must set data-selfcheck="pass"
#   2. over http, hostile ledger: markup in every field must render as text and run nothing
#   3. over http, a rotated series given as three ?ledger= files must verify
#   4. over http, 10x and 100x the sample: report load and render time
#
# Needs Google Chrome (CHROME=/path/to/chrome to override) and python3.
set -u
HERE=$(cd "$(dirname "$0")/.." && pwd)
CHROME=${CHROME:-"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"}
TMP=$(mktemp -d)
PORT=${PORT:-$((20000 + $$ % 20000))}
FAIL=0
trap 'kill $SERVER 2>/dev/null; rm -rf "$TMP"' EXIT

dom() {  # dump the DOM after scripts settle
  "$CHROME" --headless=new --disable-gpu --no-first-run --no-default-browser-check \
    --user-data-dir="$TMP/profile" --virtual-time-budget="${2:-15000}" --dump-dom "$1" 2>/dev/null
}
attr() {  # value of a data- attribute on <html>
  printf '%s' "$1" 2>/dev/null | head -c 4000 | tr ' ' '\n' | sed -n "s/^data-$2=\"\([^\"]*\)\".*/\1/p" | head -1
}
status() {  # the status line only, so text inside the page's own script cannot match
  printf '%s' "$1" 2>/dev/null | tr '\n' ' ' | sed -n 's/.*\(<div class="status"[^>]*>.*\)<div class="series".*/\1/p'
}
ok()   { echo "ok    $1"; }
bad()  { echo "FAIL  $1"; FAIL=1; }

[ -x "$CHROME" ] || { echo "Chrome not found at $CHROME"; exit 2; }

# 1. from disk
OUT=$(dom "file://$HERE/demo/index.html")
SC=$(attr "$OUT" selfcheck); N=$(attr "$OUT" selfcheck-count); P=$(attr "$OUT" selfcheck-passed)
[ "$SC" = "pass" ] && ok "self-check from disk: $P of $N" || bad "self-check from disk: '$SC' ($P of $N) failed: $(attr "$OUT" selfcheck-failed)"

# serve a copy of demo/ with big fixtures added
cp -R "$HERE/demo" "$TMP/site"
python3 - "$HERE" "$TMP/site" <<'EOF'
import os, sys
sys.path.insert(0, sys.argv[1])
from delegationledger import Ledger
sample = open(os.path.join(sys.argv[1], "demo", "sample-ledger.jsonl"), encoding="utf-8").read().splitlines()
import json
src = [json.loads(l) for l in sample]
for times in (10, 100, 116):
    path = os.path.join(sys.argv[2], f"big{times}.jsonl")
    led = Ledger(path, clock=lambda: "2026-10-01T09:00:00Z")
    for k in range(times):
        for e in src:
            t = f"{e['task_id']}-{k:03d}"
            if e["book"] == "owe":
                led.owe(t, e["agent"], e["step"], amount=e["amount"], due=e["due"])
            elif e["book"] == "ran":
                led.ran(t, e["agent"], e["step"], amount=e["amount"])
            else:
                led.close(t)
    print(f"big{times}.jsonl: {len(led.entries)} lines, {os.path.getsize(path)/1e6:.1f} MB")
EOF
(cd "$TMP/site" && exec python3 -m http.server "$PORT" --bind 127.0.0.1 >/dev/null 2>&1) &
SERVER=$!
for i in 1 2 3 4 5 6 7 8 9 10; do
  curl -s -o /dev/null "http://127.0.0.1:$PORT/index.html" && break; sleep 0.3
done
BASE="http://127.0.0.1:$PORT/index.html"

# 1b. over http the page reads the .jsonl directly
OUT=$(dom "$BASE")
[ "$(attr "$OUT" selfcheck)" = "pass" ] && ok "self-check over http" || bad "self-check over http"

# 2. hostile ledger
OUT=$(dom "$BASE?ledger=fixtures/hostile.jsonl")
if printf '%s' "$OUT" 2>/dev/null | grep -q 'data-pwned'; then bad "hostile ledger: a payload executed"; else ok "hostile ledger: no payload executed"; fi
printf '%s' "$OUT" 2>/dev/null | grep -q '&lt;img src=x onerror=' && ok "hostile ledger: payload shown as text" || bad "hostile ledger: payload text missing"
status "$OUT" | grep -q 'chain verified' && ok "hostile ledger: chain verified" || bad "hostile ledger: chain not verified"

# 3. rotated series as three files, out of order
OUT=$(dom "$BASE?ledger=fixtures/series.jsonl&ledger=fixtures/series.000001.jsonl&ledger=fixtures/series.000002.jsonl")
status "$OUT" | grep -q 'chain verified' && printf '%s' "$OUT" 2>/dev/null | grep -q 'Rotated series: 3 file' \
  && ok "rotated series verifies across 3 files" || bad "rotated series"
OUT=$(dom "$BASE?ledger=fixtures/series.jsonl&ledger=fixtures/series.000001.jsonl")
status "$OUT" | grep -q 'chain broken' && ok "series with a missing middle segment is refused" || bad "missing segment not caught"
for Q in "https://example.org/x.jsonl" "%20//example.org/x.jsonl" "%09//example.org/x.jsonl" "%5C%5Cexample.org%5Cx.jsonl" "data:text/plain,%7B%7D"; do
  OUT=$(dom "$BASE?ledger=$Q")
  if status "$OUT" | grep -q 'refused ?ledger=' && ! status "$OUT" | grep -q 'chain verified'; then
    ok "?ledger=$Q refused with a visible warning, nothing loaded"
  else bad "?ledger=$Q not refused"; fi
done
OUT=$(dom "$BASE?ledger=fixtures/missing.jsonl")
status "$OUT" | grep -q 'could not open the requested ledger' && ok "missing ?ledger= file: visible warning, no fallback to the sample" || bad "missing ?ledger= file"

# 4. timing, in real time (virtual time would freeze performance.now)
for n in 1 10 100; do
  if [ "$n" = 1 ]; then Q="timing=1"; else Q="ledger=big$n.jsonl&timing=1"; fi
  T=$(python3 "$HERE/tests/page_timing.py" "$BASE?$Q")
  if [ -n "$T" ]; then ok "${n}x sample, real time: ${T#DL done }"; else bad "${n}x sample did not finish"; fi
done
T=$(python3 "$HERE/tests/page_timing.py" "$BASE?paste=big116.jsonl&timing=1")
[ -n "$T" ] && ok "5 MB paste (116x sample), real time: ${T#DL done }" || bad "5 MB paste did not finish"

exit $FAIL
