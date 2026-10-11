#!/usr/bin/env python3
"""Load a page in headless Chrome in real time and wait for its title to start
with "DL done". Prints the title. Real wall-clock timing, unlike
--virtual-time-budget, which freezes performance.now(). Standard library only:
the title is read from Chrome's remote-debugging /json/list endpoint."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def main(url, port=int(os.environ.get("CDP_PORT", "9339")), timeout=90.0):
    prof = tempfile.mkdtemp()
    proc = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={prof}",
                             f"--remote-debugging-port={port}", "--window-size=1280,900", url],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        end = time.time() + timeout
        while time.time() < end:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as r:
                    for t in json.load(r):
                        if t.get("type") == "page" and t.get("title", "").startswith("DL done"):
                            print(t["title"])
                            return 0
            except OSError:
                pass
            time.sleep(0.2)
        print("timeout waiting for the page", file=sys.stderr)
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(prof, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
