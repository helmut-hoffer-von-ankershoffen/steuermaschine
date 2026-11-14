#!/usr/bin/env python3
"""A scripted `gh` / `bd` for the parity harness: records every call and answers from a table.

PARITY_ANSWERS: JSON list of {"tool", "needle", "out", "err", "rc"} or {"tool", "needle", "seq": [answer, ...]} (the
n-th call that matches the needle gets the n-th answer, the last one repeats). The needle is matched against the
arguments joined by one space; the longest needle wins. PARITY_TRANSCRIPT: JSON lines, one per call.
"""
import json
import os
import sys

name, argv = os.path.basename(sys.argv[0]), sys.argv[1:]
stdin = ""
if "--input" in argv:
    source = argv[argv.index("--input") + 1]
    stdin = sys.stdin.read() if source == "-" else open(source).read()
line = " ".join(argv)
answers = json.load(open(os.environ["PARITY_ANSWERS"]))
hits = sorted((a for a in answers if a["tool"] == name and a["needle"] in line), key=lambda a: len(a["needle"]), reverse=True)
transcript = os.environ["PARITY_TRANSCRIPT"]
before = [json.loads(row) for row in open(transcript)] if os.path.exists(transcript) else []
with open(transcript, "a") as handle:
    handle.write(json.dumps({"tool": name, "argv": argv, "stdin": stdin, "needle": hits[0]["needle"] if hits else None}) + "\n")
if not hits:
    sys.stderr.write(f"no fake answer for: {name} {line}\n")
    sys.exit(1)
answer = hits[0]
if "seq" in answer:
    seen = sum(1 for row in before if row["tool"] == name and row.get("needle") == answer["needle"])
    answer = answer["seq"][min(seen, len(answer["seq"]) - 1)]
out = answer.get("out", "")
sys.stdout.write(out if isinstance(out, str) else json.dumps(out))
sys.stderr.write(answer.get("err", ""))
sys.exit(answer.get("rc", 0))
