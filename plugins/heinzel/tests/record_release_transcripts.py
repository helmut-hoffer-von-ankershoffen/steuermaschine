#!/usr/bin/env python3
"""Record what the OLD release step runner does, for the parity tests of `sm release resume` and `sm release drop`.

    uv run python plugins/heinzel/tests/record_release_transcripts.py COPY_OF_THE_OLD_TOOLS [COPY_WITH_THE_DROP_PATCH]

Run it on COPIES of the old tools directory, never on the live one. For every scenario below it starts the old
script in a throwaway HOME, with a `gh` on PATH that answers from the scenario's rules (the same rules the test gives
`steuermaschine.github.testing.FakeGh`) and with PyPI answered from the scenario; it records the exit code, both
output streams, every `gh` call and the state files afterwards. Nothing real is read or written: no network, no
GitHub, no file outside the throwaway HOME. The drop scenarios need the copy with the reviewed patch applied
(research/hz-release-drop-candidate.patch); without the second argument they are skipped.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

OUT = Path(__file__).parent / "fixtures" / "release" / "transcripts"
REPO = "helmut-hoffer-von-ankershoffen/heinzel"
TIP = "f" * 40
TAIL = {"version": "0.9.40", "finished": True, "accepted": False, "acceptance_run": 4321, "freeze_token": "a" * 32}
CANDIDATE = {"version": "0.9.41", "freeze_token": "b" * 32}
DROP_ABSENT = {"data": {"repository": {"ref": None, "branch": None, "release": None, "pullRequests": {"nodes": [{"number": 812, "state": "CLOSED", "headRefName": "release/v0.9.41"}]}}}}
DROP_OPEN = {"data": {"repository": {"ref": None, "branch": {"name": "release/v0.9.41"}, "release": None,
                                     "pullRequests": {"nodes": [{"number": 812, "state": "OPEN", "headRefName": "release/v0.9.41"}]}}}}
NO_RUNS = {"total_count": 0, "workflow_runs": []}
SEQ = "__in_order__"  # a rule whose value is {SEQ: [first, second, ...]} answers in that order (the last one repeats)

# name -> (tools copy: 0 old, 1 with the drop patch; arguments of the old script; files in ~/.cache before; gh rules; PyPI status per URL part)
SCENARIOS: dict[str, dict] = {
    "resume-main-red": {
        "tools": 0, "argv": ["0.9.41", "--timeout", "1"], "state": {},
        "gh": {
            f"api repos/{REPO}/releases?per_page=30": "v0.9.40\nv0.9.39\n",
            f"api repos/{REPO}/git/ref/tags/v0.9.41": 404,
            f"api repos/{REPO}/commits/main": TIP + "\n",
            "run list": [{"databaseId": 900, "status": "completed", "conclusion": "failure", "createdAt": "2026-11-12T18:00:00Z", "headSha": TIP, "displayTitle": "feat: x", "attempt": 1}],
            f"api repos/{REPO}/actions/runs/900/jobs": {"jobs": [{"name": "Lint", "conclusion": "failure", "runner_id": 7, "steps": [{"name": "run"}]}]},
        },
        "pypi": {},
    },
    "absorb": {
        "tools": 0, "argv": ["--absorb", "0.9.40", "--reason", "fixed in 0.9.41 by #812"], "state": {"hz-release/v0.9.40.json": TAIL},
        "gh": {"pr list": [{"headRefName": "release/v0.9.41"}, {"headRefName": "feat/x"}]},
        "pypi": {},
    },
    "absorb-accepted-refused": {
        "tools": 0, "argv": ["--absorb", "0.9.40", "--reason", "fixed in 0.9.41 by #812"], "state": {"hz-release/v0.9.40.json": TAIL | {"accepted": True}},
        "gh": {}, "pypi": {},
    },
    "drop": {
        "tools": 1, "argv": ["0.9.41", "--drop-candidate", "--reason", "candidate red: Coverage, fix in #812"],
        "state": {"hz-release.json": CANDIDATE, "hz-release-holds/" + "b" * 32: "0.9.41"},
        "gh": {"api graphql": DROP_ABSENT, f"api repos/{REPO}/actions/runs?branch=release/v0.9.41": NO_RUNS},
        "pypi": {"heinzel/0.9.41": 404},
    },
    "drop-close-pr": {
        "tools": 1, "argv": ["0.9.41", "--drop-candidate", "--reason", "candidate red: Coverage, fix in #812", "--close-pr"],
        "state": {"hz-release.json": CANDIDATE, "hz-release-holds/" + "b" * 32: "0.9.41", "hz-release-holds/" + "c" * 32: "0.9.42"},
        "gh": {"api graphql": {SEQ: [DROP_OPEN, DROP_ABSENT]}, f"api repos/{REPO}/actions/runs?branch=release/v0.9.41": NO_RUNS,
               "pr close 812": "", f"api --method DELETE repos/{REPO}/git/refs/heads/release/v0.9.41": ""},
        "pypi": {"heinzel/0.9.41": 404},
    },
    "drop-published-refused": {
        "tools": 1, "argv": ["0.9.41", "--drop-candidate", "--reason", "candidate red: Coverage, fix in #812"],
        "state": {"hz-release.json": CANDIDATE, "hz-release-holds/" + "b" * 32: "0.9.41"},
        "gh": {"api graphql": DROP_ABSENT, f"api repos/{REPO}/actions/runs?branch=release/v0.9.41": NO_RUNS},
        "pypi": {"heinzel/0.9.41": 200},
    },
}

FAKE_GH = '''#!{python}
import json, os, sys
from steuermaschine.github.testing import FakeGh
gh, counts_file = FakeGh(), os.environ["FAKE_GH_RULES"] + ".counts"
counts = json.load(open(counts_file)) if os.path.exists(counts_file) else {{}}
def pick(needle, answer):
    if isinstance(answer, dict) and "{seq}" in answer:
        def nth(argv, needle=needle, answers=answer["{seq}"]):
            counts[needle] = counts.get(needle, 0) + 1
            return answers[min(counts[needle], len(answers)) - 1]
        return nth
    return answer
for needle, answer in json.load(open(os.environ["FAKE_GH_RULES"])).items():
    gh.on(needle, pick(needle, answer))
done = gh.run(["gh", *sys.argv[1:]])
json.dump(counts, open(counts_file, "w"))
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
sys.stdout.write(done.out); sys.stderr.write(done.err); sys.exit(done.rc)
'''
FAKE_NET = '''import json, os, urllib.error, urllib.request
_rules = json.loads(os.environ.get("FAKE_PYPI", "{}"))
class _Reply:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def read(self, *a): return b"{}"
def _urlopen(request, timeout=None, **kwargs):
    url = request if isinstance(request, str) else request.full_url
    status = next((code for part, code in _rules.items() if part in url), 404)
    if status != 200:
        raise urllib.error.HTTPError(url, status, "scripted", {}, None)
    return _Reply()
urllib.request.urlopen = _urlopen
'''


def seed(cache: Path, files: dict) -> None:
    for name, content in files.items():
        path = cache / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(content if isinstance(content, str) else json.dumps(content))


def snapshot(cache: Path) -> dict:
    """Every file below the throwaway ~/.cache: JSON parsed, other text as is; lock files only by name."""
    out = {}
    for path in sorted(p for p in cache.rglob("*") if p.is_file()):
        name = str(path.relative_to(cache))
        text = path.read_text()
        try:
            out[name] = json.loads(text) if name.endswith(".json") else text
        except ValueError:
            out[name] = text
    return out


def record(name: str, spec: dict, tools: Path) -> dict:
    with tempfile.TemporaryDirectory() as scratch:
        home, bin_dir, site = Path(scratch) / "home", Path(scratch) / "bin", Path(scratch) / "site"
        for path in (home / ".cache", bin_dir, site):
            path.mkdir(parents=True)
        (home / "Code/heinzel-management").mkdir(parents=True)
        (home / "Code/heinzel-management/AGENTS.md").write_text("## 9. Log\n")
        seed(home / ".cache", spec["state"])
        (bin_dir / "gh").write_text(FAKE_GH.format(python=sys.executable, seq=SEQ))
        (bin_dir / "gh").chmod(0o755)
        (site / "sitecustomize.py").write_text(FAKE_NET)
        rules, log = Path(scratch) / "rules.json", Path(scratch) / "gh.log"
        rules.write_text(json.dumps(spec["gh"]))
        log.write_text("")
        env = {"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin", "PYTHONPATH": str(site), "FAKE_GH_RULES": str(rules), "FAKE_GH_LOG": str(log),
               "FAKE_PYPI": json.dumps(spec["pypi"]), "HZ_RELEASE_FLOW_DIR": str(home / ".cache/flow"), "HZ_ALARM_NOTIFY": "off", "PYTHONDONTWRITEBYTECODE": "1"}
        done = subprocess.run([sys.executable, str(tools / "release"), *spec["argv"]], env=env, capture_output=True, text=True, timeout=120, check=False)
        return {"name": name, "old_argv": spec["argv"], "state_before": spec["state"], "gh_rules": spec["gh"], "pypi": spec["pypi"], "exit": done.returncode,
                "stdout": done.stdout.replace(str(home), "~").splitlines(), "stderr": done.stderr.replace(str(home), "~").splitlines(),
                "gh_calls": [json.loads(line) for line in log.read_text().splitlines()], "state_after": snapshot(home / ".cache")}


def main() -> int:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    copies = [Path(arg).expanduser().resolve() for arg in sys.argv[1:3]]
    if any(str(Path("~/Code/heinzel-management").expanduser()) in str(copy) for copy in copies):
        sys.exit("refusing: record on a COPY of the old tools, never on the live checkout")
    OUT.mkdir(parents=True, exist_ok=True)
    for name, spec in SCENARIOS.items():
        if spec["tools"] >= len(copies):
            print(f"skipped {name}: needs the copy with the drop patch")
            continue
        result = record(name, spec, copies[spec["tools"]])
        (OUT / f"{name}.json").write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
        print(f"recorded {name}: exit {result['exit']}, {len(result['gh_calls'])} gh call(s), {len(result['stdout'])} line(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
