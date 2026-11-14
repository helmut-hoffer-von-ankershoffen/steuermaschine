import os
import subprocess
import types

import pytest
from packet_paths import tool  # noqa: F401  (the fixture that imports a moved module)

LIMITS = {"python": 32, "default": 10, "production": 100}


def fact(branch, *, python=(), web=(), browser=(), production=(), protected=(), changed=(), conflict=""):
    selection = {k: list(v) for k, v in (("python", python), ("web", web), ("browser", browser)) if v}
    return {"branch": branch, "head": branch.ljust(40, "0"), "conflict": conflict, "changed": {p: "M" for p in changed or production},
            "selection": selection, "production": list(production), "protected": list(protected), "reasons": []}


def ids(prefix, n):
    return [f"web/{prefix}{i}.test.ts" for i in range(n)]


def test_wave_that_fits_is_accepted(tool):
    wave = tool("wave_size")
    problems, over, lanes, prod = wave.evaluate([fact("a", web=ids("a", 6)), fact("b", web=ids("b", 4), python=["tests/x_test.py"])], LIMITS)
    assert problems == [] and over == {} and len(lanes["web"]) == 10


def test_union_over_the_web_limit_is_refused_and_names_a_branch(tool):
    wave = tool("wave_size")
    facts = [fact("a", web=ids("a", 6)), fact("b", web=ids("b", 6)), fact("c", python=["tests/c_test.py"])]
    problems, over, *_ = wave.evaluate(facts, LIMITS)
    assert over == {"web": (12, 10)} and problems == ["union web: 12 > 10"]
    assert wave.to_move(facts, LIMITS) in {"a", "b"} and wave.to_move(facts, LIMITS) != "c"


def test_shared_ids_count_once_and_python_limit_is_32(tool):
    wave = tool("wave_size")
    same = ids("s", 8)
    assert wave.evaluate([fact("a", web=same), fact("b", web=same)], LIMITS)[0] == []
    many = [f"tests/t{i}_test.py" for i in range(33)]
    assert wave.evaluate([fact("a", python=many)], LIMITS)[1] == {"python": (33, 32)}


WHOLE = {**LIMITS, "vitest_all": "web/ALL"}


def test_whole_vitest_suite_counts_as_one_web_id(tool):
    wave = tool("wave_size")
    facts = [fact("a", web=["web/ALL"]), fact("b", web=ids("b", 6))]
    problems, over, lanes, _ = wave.evaluate(facts, WHOLE)
    assert problems == [] and over == {} and lanes["web"] == {"web/ALL"}
    text = "\n".join(wave.report(facts, [], WHOLE))
    assert "web=1 (whole suite)" in text and "web=1/10 (whole suite)" in text


def test_web_union_over_the_cap_becomes_the_whole_suite_only_with_the_base_sentinel(tool):
    wave = tool("wave_size")
    facts = [fact("a", web=ids("a", 6)), fact("b", web=ids("b", 6))]
    assert wave.evaluate(facts, WHOLE)[2]["web"] == {"web/ALL"} and wave.evaluate(facts, WHOLE)[0] == []
    assert wave.evaluate(facts, LIMITS)[1] == {"web": (12, 10)}  # older base: still over the limit
    browser = [fact("a", browser=ids("a", 6)), fact("b", browser=ids("b", 6))]
    assert wave.evaluate(browser, WHOLE)[1] == {"browser": (12, 10)}  # only the Vitest lane has a whole suite


def test_production_limit_and_conflict_are_problems(tool):
    wave = tool("wave_size")
    big = [f"src/heinzel/m{i}.py" for i in range(101)]
    assert wave.evaluate([fact("a", production=big)], LIMITS)[1] == {"production": (101, 100)}
    assert wave.evaluate([fact("a", conflict="base")], LIMITS)[0] == ["a: conflicts with the base (base)"]


def test_overlaps_and_report(tool):
    wave = tool("wave_size")
    a, b = fact("a", changed=["x.py", "y.py"], protected=["uv.lock"]), fact("b", changed=["y.py"])
    assert wave.overlaps([a, b]) == {("a", "b"): ["y.py"]}
    text = "\n".join(wave.report([a, b], [("a", "b")], LIMITS))
    assert "protected=1 uv.lock" in text and "overlap a x b: 1 file(s): y.py" in text and "CONFLICT a x b" in text


def test_run_exit_codes_with_fakes(tool, monkeypatch):
    wave = tool("wave_size")

    class Repo:
        def __init__(self, path): self.path = path
        def commit(self, rev): return "b" * 40

    class Policy:
        python_max_ids, max_ids, protected = 32, 10, ()

    monkeypatch.setattr(wave, "fm", types.SimpleNamespace(Repo=Repo, Policy=lambda repo, base: Policy(), MAX_COVERED_FILES=100))
    out = []
    gathered = {"a": fact("a", web=ids("a", 6)), "b": fact("b", web=ids("b", 6))}
    gather = lambda repo, base, branch, policy: gathered[branch]
    assert wave.run(["/x/core:a", "/x/core:b"], "/x", "origin/main", out.append, gather, lambda r, f: []) == 1
    assert any(line.startswith("REFUSED") for line in out) and any(line.startswith("MOVE ") for line in out)
    out.clear()
    assert wave.run(["a"], "/x", "origin/main", out.append, gather, lambda r, f: []) == 0 and out[-1] == "FITS"
    out.clear()
    small = {"a": gathered["a"], "b": fact("b", web=ids("c", 1))}
    assert wave.run(["a", "b"], "/x", "origin/main", out.append, lambda r, b_, br, p: small[br], lambda r, f: [("a", "b")]) == 1
    assert "CONFLICT a x b" in out and "MOVE b to the next wave" in out[-1]


def test_pair_conflicts_with_scratch_repo(tool, tmp_path):
    wave = tool("wave_size")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
    def git(*a):
        return subprocess.run(["git", "-C", str(tmp_path), *a], env=env, capture_output=True, text=True, check=True).stdout.strip()
    git("init", "-q", "-b", "main")
    (tmp_path / "f").write_text("0\n"); git("add", "f"); git("commit", "-qm", "0")
    heads = {}
    for name, text in (("a", "1\n"), ("b", "2\n"), ("c", "0\nx\n")):
        git("checkout", "-q", "-b", name, "main")
        (tmp_path / "f" if name != "c" else tmp_path / "g").write_text(text); git("add", "-A"); git("commit", "-qm", name)
        heads[name] = git("rev-parse", "HEAD")

    class Repo:
        def git(self, *args, check=True):
            return subprocess.run(["git", "-C", str(tmp_path), *args], env=env, capture_output=True)
    facts = [{"branch": n, "head": h, "conflict": ""} for n, h in heads.items()]
    assert wave.pair_conflicts(Repo(), facts) == [("a", "b")]

