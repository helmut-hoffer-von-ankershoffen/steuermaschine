"""sm plugin gallery and gallery-check (bead jbgo): the drift checks read from a fixture heinzel-plugins worktree, the
frozen argv and allowlisted environment that reach the run lock, the push block, the remote-ref comparison and the
changed-image diff. No browser, make, uv or agent runs; the review's three bypass probes are tests here."""

import os
import subprocess
from pathlib import Path

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.engine import Refuse, Wait
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel.native import plugin_gallery as pg
from steuermaschine_heinzel.plugin import plugin

FROZEN = " ".join(pg.FROZEN)
MAKEFILE = f"""PLUGIN := {FROZEN}
ifneq ($(origin TOOLING),undefined)
export HZ_PLUGIN_TOOLING := $(value TOOLING)
PLUGIN := uv run --quiet --no-project --with "$$HZ_PLUGIN_TOOLING" hz-plugin
endif
screenshots:
\t@$(PLUGIN) screenshots $(if $(OPEN),--open) $(if $(JSON),--json)
screenshots-check:
\t@$(PLUGIN) screenshots-check
"""
CAPTURE = "\n".join(needle for _, needle in pg.REQUIRED) + "\nimport heinzel_plugin_tools.vendor\n"


def tree(tmp_path, makefile=MAKEFILE, capture=CAPTURE, vendor="def fetch(): pass\n", plan=True):
    root = tmp_path / "plugins-wt"
    (root / "src/heinzel_plugin_tools").mkdir(parents=True)
    (root / "Makefile").write_text(makefile)
    (root / pg.CAPTURE).write_text(capture)
    (root / pg.TOOLING / "vendor.py").write_text(vendor)
    shots = root / "plugins/heinzel-clock/docs/assets/screenshots"
    shots.mkdir(parents=True)
    (shots / "old.png").write_bytes(b"old")
    (shots / "same.png").write_bytes(b"same")
    (shots / "gone.png").write_bytes(b"gone")
    if plan:
        (root / "plugins/heinzel-clock/docs/screenshots.toml").write_text("[[scenes]]\n")
    return root


def rt(table=None):
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": table or {}}, Path("."))
    return Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"]), stack, settings, _providers=fake_providers())


def with_runtime(monkeypatch, refs=("refs/remotes/origin/main abc",)):
    runtime = rt()
    monkeypatch.setattr(pg, "runtime", lambda: type("R", (), {"settings": runtime.settings, "providers": fake_providers()})())
    answers = list(refs)
    monkeypatch.setattr(pg, "pushurls", lambda repo: "")
    monkeypatch.setattr(pg, "remote_refs", lambda repo: answers.pop(0) if len(answers) > 1 else answers[0])


def test_recipe_and_assignments():
    assert pg.recipe(MAKEFILE, "screenshots") == ["@$(PLUGIN) screenshots $(if $(OPEN),--open) $(if $(JSON),--json)"]
    assert pg.recipe("X := 1\n", "screenshots") is None
    assert pg.recipe("screenshots := 1\n", "screenshots") is None
    assert pg.assignments("override PLUGIN = a\n  export PLUGIN ?= b\nPLUGINS := c\n", "PLUGIN") == ["a", "b"]


def test_conditions_met(tmp_path):
    met, problems = pg.conditions(tree(tmp_path), "clock")
    assert problems == [] and len(met) == 6 and met[0].startswith("command:")


@pytest.mark.parametrize("makefile, why", [
    ("help:\n\techo\n", "PLUGIN is not the frozen capture command"),
    (MAKEFILE.replace(f"PLUGIN := {FROZEN}", f"PLUGIN := {FROZEN} --headed"), "PLUGIN is not the frozen capture command"),  # review probe 1
    (MAKEFILE + "override PLUGIN += --headed\n", "PLUGIN is not the frozen capture command"),
    (MAKEFILE.replace("--json)", "--json) --headed"), "not the plain capture call"),
    (MAKEFILE.replace("$(if $(JSON),--json)", "$(if $(JSON),--headed)"), "not the plain capture call"),
    (MAKEFILE.replace("\t@$(PLUGIN) screenshots $", "\tgit push\n\t@$(PLUGIN) screenshots $"), "not the plain capture call"),
    (MAKEFILE.replace("screenshots:\n", "other:\n"), "no screenshots target"),
])
def test_makefile_drift_refuses(tmp_path, makefile, why):
    _, problems = pg.conditions(tree(tmp_path, makefile=makefile), "clock")
    assert any(why in p for p in problems), problems


def test_capture_drift_refuses(tmp_path):
    capture = CAPTURE.replace("headless=not headed", "headless=False")
    _, problems = pg.conditions(tree(tmp_path, capture=capture), "clock")
    assert problems == ["headless: src/heinzel_plugin_tools/capture.py no longer contains 'headless=not headed'"]
    _, problems = pg.conditions(tree(tmp_path / "c", plan=False), "clock")
    assert problems == ["heinzel-clock has no screenshot plan (plugins/heinzel-clock/docs/screenshots.toml)"]
    _, problems = pg.conditions(tmp_path / "nothing", "clock")
    assert problems[0].startswith("not a heinzel-plugins worktree")


@pytest.mark.parametrize("vendor", ['subprocess.run(["git", "push"])\n', "os.system('git push origin')\n",
                                    "git('-c', 'remote.origin.pushurl=x')\n"])
def test_a_push_in_any_tooling_module_refuses(tmp_path, vendor):  # review probe 2: an imported module, not capture.py
    _, problems = pg.conditions(tree(tmp_path, vendor=vendor), "clock")
    assert problems == ["no push: src/heinzel_plugin_tools/vendor.py mentions a push"]


def test_environment_is_an_allowlist_and_blocks_pushes():  # review probe 3: OPEN, TOOLING, MAKEFLAGS never reach the capture
    hostile = {"PATH": "/bin", "HOME": "/h", "OPEN": "1", "TOOLING": "evil", "MAKEFLAGS": "PLUGIN=x --headed", "MFLAGS": "-x",
               "GNUMAKEFLAGS": "-x", "PLUGIN_CORE": "evil.whl", "HZ_PLUGIN_TOOLING": "evil", "GH_TOKEN": "t", "GIT_DIR": "/x"}
    env = pg.capture_environment(hostile, "clock")
    assert {k for k in env if not k.startswith("GIT_CONFIG_")} == {"PATH", "HOME", "PLUGIN_NAME", "GIT_TERMINAL_PROMPT"}
    assert env["PLUGIN_NAME"] == "clock" and env["GIT_CONFIG_COUNT"] == "1" and env["GIT_CONFIG_VALUE_0"] == ""
    assert pg.capture_argv("clock") == [*pg.FROZEN, "screenshots", "clock", "--json"]
    assert not {"make", "--headed", "--open"} & set(pg.capture_argv("clock"))


def git_repos(tmp_path):
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "-c", "user.email=a@example.com", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    return remote, work


def blocked_push(work, target, env):
    return subprocess.run(["git", "-C", str(work), "push", target, "HEAD:refs/heads/main"], env=env, capture_output=True, timeout=60)


@pytest.mark.parametrize("relative", [False, True])  # review round 2 (a): a relative path was not covered by prefixes
def test_the_push_block_stops_a_real_git_push(tmp_path, relative):
    remote, work = git_repos(tmp_path)
    env = pg.capture_environment(dict(os.environ), "clock")
    assert blocked_push(work, "../remote.git" if relative else str(remote), env).returncode != 0
    assert subprocess.run(["git", "ls-remote", str(remote)], capture_output=True, text=True).stdout == ""


def test_the_push_block_rewrites_scp_like_urls(tmp_path):  # review round 2 (b): never reaches the real host
    _, work = git_repos(tmp_path)
    env = pg.capture_environment(dict(os.environ), "clock")
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", "github.com:example/none.git"], check=True)
    url = subprocess.run(["git", "-C", str(work), "remote", "get-url", "--push", "origin"], env=env, capture_output=True, text=True).stdout
    assert url.strip() == pg.PUSH_BLOCK + "github.com:example/none.git"


class RealGit:
    def run(self, argv, timeout=None):
        res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return type("R", (), {"rc": res.returncode, "out": res.stdout})()


def test_a_pushurl_refuses_before_the_run(tmp_path, monkeypatch):  # review round 2 (c): pushInsteadOf skips a pushurl
    remote, work = git_repos(tmp_path)
    monkeypatch.setattr(pg, "runtime", lambda: type("R", (), {"providers": type("P", (), {"commands": RealGit()})()})())
    assert pg.pushurls(work) == ""
    subprocess.run(["git", "-C", str(work), "config", "remote.origin.pushurl", str(remote)], check=True)
    assert pg.pushurls(work) == f"remote.origin.pushurl {remote}"
    assert pg.pushurls(tmp_path / "missing") is None
    subprocess.run(["git", "-C", str(work), "config", "--unset", "remote.origin.pushurl"], check=True)
    subprocess.run(["git", "-C", str(work), "config", f"url.{remote}/.pushInsteadOf", "zz:"], check=True)  # review round 3
    assert pg.pushurls(work) == f"url.{remote}/.pushinsteadof zz:"
    subprocess.run(["git", "-C", str(work), "config", "url.elsewhere:/.insteadOf", "yy:"], check=True)  # a plain insteadOf is fine
    assert pg.pushurls(work) == f"url.{remote}/.pushinsteadof zz:"
    root = tree(tmp_path / "t")
    with_runtime(monkeypatch)
    monkeypatch.setattr(pg, "run_capture", lambda *a: pytest.fail("ran"))
    for answer, why in ((f"remote.origin.pushurl {remote}", "a remote.*.pushurl or url.*.pushInsteadOf is configured"), (None, "cannot read the git configuration")):
        monkeypatch.setattr(pg, "pushurls", lambda repo, answer=answer: answer)
        out = pg.plugin_gallery("clock", root)
        assert out.exit_code == 1 and not out.ran and out.problems[0].startswith(why)


def test_changed_images_and_capture_error(tmp_path):
    before = {str(tmp_path / "a.png"): "1", str(tmp_path / "b.png"): "2", str(tmp_path / "c.png"): "3"}
    after = {str(tmp_path / "a.png"): "1", str(tmp_path / "b.png"): "9", str(tmp_path / "d.png"): "4"}
    assert pg.changed_images(before, after, tmp_path) == ["b.png changed", "c.png removed", "d.png added"]
    assert pg.images(tmp_path / "missing") == {}
    log = tmp_path / "log"
    assert pg.capture_error(log) == ""
    log.write_text('progress\n{"ok": false, "error": "Agent review did not complete"}\n')
    assert pg.capture_error(log) == "Agent review did not complete"
    log.write_text('{"ok": true}\n{broken\n')
    assert pg.capture_error(log) == ""


def test_check_is_read_only_and_names_the_next_command(tmp_path):
    root = tree(tmp_path)
    out = invoke(rt(), ["plugin", "gallery-check", "heinzel-clock", "--repo", str(root)])
    assert out.exit_code == 0, out
    lines = out.stdout.splitlines()
    assert lines[-1] == f"NEXT   sm plugin gallery clock --repo {root}" and lines[-2].startswith("PLAN ")
    assert invoke(rt({"gallery_plugins_repo": str(root)}), ["plugin", "gallery-check", "clock"]).exit_code == 0
    assert invoke(rt(), ["plugin", "gallery-check", "clock"]).exit_code == 2  # no worktree
    assert invoke(rt(), ["plugin", "gallery-check", "Bad Name", "--repo", str(root)]).exit_code == 2
    refused = invoke(rt(), ["plugin", "gallery-check", "clock", "--repo", str(tmp_path / "nothing")])
    assert refused.exit_code == 1 and "REFUSE not a heinzel-plugins worktree" in refused.stdout


def fake_capture(code=0, why="", error=""):
    calls = []

    def run(repo, plugin, log):
        calls.append((repo, plugin))
        shots = repo / "plugins" / f"heinzel-{plugin}" / "docs/assets/screenshots"
        (shots / "old.png").write_bytes(b"new")
        (shots / "gone.png").unlink()
        (shots / "added.png").write_bytes(b"added")
        log.write_text(f'{{"ok": false, "error": "{error}"}}\n' if error else '{"ok": true}\n')
        return code, why
    return run, calls


def test_gallery_lists_changed_images_and_requires_the_review(tmp_path, monkeypatch):
    root = tree(tmp_path)
    run, calls = fake_capture()
    monkeypatch.setattr(pg, "run_capture", run)
    with_runtime(monkeypatch)
    out = pg.plugin_gallery("clock", root)
    assert calls == [(root.resolve(), "clock")] and out.exit_code == 0
    base = "plugins/heinzel-clock/docs/assets/screenshots/"
    assert out.changed == [base + "added.png added", base + "gone.png removed", base + "old.png changed"]
    text = "\n".join(out.render())
    assert "REVIEW privacy review of 3 image(s) required before merge" in text and f"RAN    {FROZEN} screenshots clock --json" in text
    assert out.next.startswith("an independent privacy review")


def test_moved_remote_refs_fail_the_run(tmp_path, monkeypatch):
    root = tree(tmp_path)
    monkeypatch.setattr(pg, "run_capture", fake_capture()[0])
    with_runtime(monkeypatch, refs=("refs/remotes/origin/main abc", "refs/remotes/origin/main abc\nrefs/remotes/origin/x def"))
    out = pg.plugin_gallery("clock", root)
    assert out.exit_code == 1 and out.problems[0].startswith("the remote-tracking refs changed") and out.next.startswith("tell the coordinator")


def test_unreadable_refs_run_nothing(tmp_path, monkeypatch):
    root = tree(tmp_path)
    monkeypatch.setattr(pg, "run_capture", lambda *a: pytest.fail("ran"))
    with_runtime(monkeypatch, refs=(None,))
    out = pg.plugin_gallery("clock", root)
    assert out.exit_code == 1 and not out.ran


def test_gallery_failure_and_lock(tmp_path, monkeypatch):
    root = tree(tmp_path)
    with_runtime(monkeypatch)
    run, _ = fake_capture(code=1, error="Agent review did not complete")
    monkeypatch.setattr(pg, "run_capture", run)
    out = pg.plugin_gallery("clock", root)
    assert out.exit_code == 1 and out.error == "Agent review did not complete" and out.review_required
    assert "ERROR  Agent review did not complete" in out.render()
    monkeypatch.setattr(pg, "run_capture", lambda repo, plugin, log: (75, "load too high"))
    with pytest.raises(Wait):
        pg.plugin_gallery("clock", root)
    monkeypatch.setattr(pg, "run_capture", lambda repo, plugin, log: (2, "refused"))
    with pytest.raises(Refuse):
        pg.plugin_gallery("clock", root)
    monkeypatch.setattr(pg, "run_capture", lambda repo, plugin, log: (0, ""))
    out = pg.plugin_gallery("clock", root)
    assert out.changed == [] and "IMAGE  none changed" in out.render() and out.next.startswith("nothing to review")


def test_gallery_runs_nothing_when_a_condition_fails(tmp_path, monkeypatch):
    root = tree(tmp_path, plan=False)
    with_runtime(monkeypatch)
    monkeypatch.setattr(pg, "run_capture", lambda *a: pytest.fail("ran"))
    out = pg.plugin_gallery("clock", root)
    assert out.exit_code == 1 and not out.ran


def test_run_capture_sends_the_frozen_argv_and_allowlist_to_the_lock(tmp_path, monkeypatch):
    seen = {}

    class Gate:
        commands = None

        def run(self, request):
            seen.update(argv=list(request.argv), env=dict(request.environ), cwd=request.cwd, timeout=request.timeout_seconds)
            return type("O", (), {"status": "ran", "exit_code": 0, "reason": ""})()
    monkeypatch.setattr(pg, "lock_of", lambda: Gate())
    for name, value in {"OPEN": "1", "TOOLING": "evil", "MAKEFLAGS": "PLUGIN=x --headed"}.items():
        monkeypatch.setenv(name, value)
    with_runtime(monkeypatch)
    assert pg.run_capture(tmp_path, "clock", tmp_path / "log") == (0, "")
    assert seen["argv"] == [*pg.FROZEN, "screenshots", "clock", "--json"] and seen["cwd"] == str(tmp_path)
    assert not {"OPEN", "TOOLING", "MAKEFLAGS"} & set(seen["env"]) and seen["timeout"] == pg.TIMEOUT_SECONDS


def test_remote_refs_reads_git(tmp_path, monkeypatch):
    class Cmds:
        def __init__(self, rc):
            self.rc = rc

        def run(self, argv, timeout=None):
            self.argv = argv
            return type("R", (), {"rc": self.rc, "out": "refs/remotes/origin/main abc\n"})()
    for rc, want in ((0, "refs/remotes/origin/main abc\n"), (128, None)):
        cmds = Cmds(rc)
        monkeypatch.setattr(pg, "runtime", lambda cmds=cmds: type("R", (), {"providers": type("P", (), {"commands": cmds})()})())
        assert pg.remote_refs(tmp_path) == want
        assert cmds.argv[:3] == ["git", "-C", str(tmp_path)] and cmds.argv[-1] == "refs/remotes"


def test_logged_sends_output_to_the_log(tmp_path):
    class Inner:
        def run(self, *a, **k):
            return "ran"

        def passthrough(self, argv, cwd=None, env=None, timeout=None):
            self.argv = argv
            return 3
    inner = Inner()
    logged = pg.Logged(inner, tmp_path / "log")
    assert logged.run(["x"]) == "ran" and logged.passthrough(["make"]) == 3
    assert inner.argv[:4] == ["sh", "-c", 'exec "$@" >"$0" 2>&1', str(tmp_path / "log")]
