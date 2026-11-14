"""Scope only reviewed release-PR pushes; reject generated release diffs outside that scope."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest


def prepare(tool, monkeypatch, tmp_path):
    release = tool("hz-release")
    monkeypatch.setattr(release, "release_pr", lambda _: None)
    monkeypatch.setenv("HZ_RELEASE_DIR", str(tmp_path / "release-work"))
    (tmp_path / "release-work/scripts").mkdir(parents=True)
    (tmp_path / "release-work/scripts/release_notes.py").write_text("")  # the cut reads it to see whether it takes --ref
    (tmp_path / "release-work/CHANGELOG.md").write_text("## v0.9.17\n")  # the notes audit of the cut reads it (it runs, unstubbed)
    return release


def test_release_pr_prepares_locally_then_uses_scoped_normal_push(tool, monkeypatch, tmp_path):
    release = prepare(tool, monkeypatch, tmp_path)
    calls = []
    checked = "b" * 40
    pushed = False

    def run(command, **kwargs):
        nonlocal pushed
        calls.append((command, kwargs))
        if command[0] == "git" and "rev-parse" in command:
            if "-q" in command:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            output = "release/v0.9.17\n" if "--abbrev-ref" in command else checked + "\n"
            return SimpleNamespace(returncode=0, stdout=output, stderr="")
        if command[0] == "git" and "ls-remote" in command:
            output = checked + "\trefs/heads/release/v0.9.17\n" if pushed else ""
            return SimpleNamespace(returncode=0, stdout=output, stderr="")
        if command[0] == "git":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command == [*release.edge.gate(), "bash", "scripts/release.sh", "patch"]:
            return SimpleNamespace(returncode=0, stdout="v0.9.17\n", stderr="")
        if "push" in command:
            pushed = True
        return SimpleNamespace(returncode=0, stdout="https://github.com/example/heinzel/pull/700\n", stderr="")

    monkeypatch.setattr(release.subprocess, "run", run)
    monkeypatch.setattr(release, "verify_release_pr_scope", lambda *_: checked)
    release.open_release_pr("v0.9.17", "patch", "Preserve update hashes")

    work = str(tmp_path / "release-work")
    gate = release.edge.gate()
    script_call = next((command, kwargs) for command, kwargs in calls if "scripts/release.sh" in command)
    assert script_call == ([*gate, "bash", "scripts/release.sh", "patch"], {
        "cwd": work, "env": os.environ | {"INTRO": "Preserve update hashes"},
        "capture_output": True, "text": True, "check": False,
    })
    push_call = next((command, kwargs) for command, kwargs in calls if command[:len(gate) + 1] == [*gate, "git"])
    assert push_call[0] == [*gate, "git", "push", "--quiet", "origin", f"{checked}:refs/heads/release/v0.9.17"]
    assert {name: push_call[1]["env"][name] for name in release.RELEASE_PR_PUSH_ENV} == release.RELEASE_PR_PUSH_ENV
    assert not any("--open-pr" in command for command, _ in calls)
    assert ["git", "-C", work, "pull", "--ff-only", "-q"] in [command for command, _ in calls]
    assert any(command[:3] == ["gh", "pr", "create"] for command, _ in calls)
    commands = [command for command, _ in calls]
    assert max(i for i, command in enumerate(commands) if "ls-remote" in command) < next(
        i for i, command in enumerate(commands) if command[:3] == ["gh", "pr", "create"]
    )


def test_unknown_release_path_fails_closed_before_push(tool, monkeypatch, tmp_path):
    release = tool("hz-release")
    outputs = iter([
        (0, "release/v0.9.17\n"),
        (0, ""),
        (0, "a" * 40 + "\n"),
        (0, "a" * 40 + "\n"),
        (0, "b" * 40 + "\n"),
        (0, "b" * 40 + " " + "a" * 40 + "\n"),
        (0, f":100644 100644 {'c' * 40} {'d' * 40} M\0README.md\0"),
    ])
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        code, output = next(outputs)
        return SimpleNamespace(returncode=code, stdout=output, stderr="")

    monkeypatch.setattr(release.subprocess, "run", run)
    with pytest.raises(release.Stop, match="outside the reviewed release-only scope"):
        release.verify_release_pr_scope(str(tmp_path), "v0.9.17")
    assert not any("push" in command for command in calls)
    assert not any(command[:2] == ["gh", "pr"] for command in calls)


def test_existing_release_branch_is_preserved_and_never_deleted(tool, monkeypatch, tmp_path):
    release = prepare(tool, monkeypatch, tmp_path)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if "rev-parse" in command:
            return SimpleNamespace(returncode=0, stdout="abc\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(release.subprocess, "run", run)
    with pytest.raises(release.Stop, match="already exists; it was preserved"):
        release.prepare_release_pr(str(tmp_path / "release-work"), "v0.9.17", "patch", "intro")
    assert not any("switch" in command or "branch" in command for command in calls)
    assert not any("push" in command for command in calls)


@pytest.mark.parametrize("failure", ["head-before-push", "head-during-push", "remote-mismatch", "remote-missing", "remote-error", "remote-exists"])
def test_release_publication_binds_checked_head_and_remote(tool, monkeypatch, tmp_path, failure):
    release = prepare(tool, monkeypatch, tmp_path)
    checked = "b" * 40
    current = checked
    pushed = False
    calls = []
    monkeypatch.setattr(release, "prepare_release_pr", lambda *_: checked)

    def run(command, **kwargs):
        nonlocal current, pushed
        calls.append(command)
        output, code = "", 0
        if command[0] == "uv":
            output = "Release notes\n"
            if failure == "head-before-push":
                current = "c" * 40
        elif "rev-parse" in command:
            output = "release/v0.9.35\n" if "--abbrev-ref" in command else current + "\n"
        elif "push" in command:
            pushed = True
            if failure == "head-during-push":
                current = "c" * 40
        elif "ls-remote" in command:
            output = checked + "\trefs/heads/release/v0.9.35\n" if pushed or failure == "remote-exists" else ""
            if pushed and failure == "remote-mismatch":
                output = "c" * 40 + "\trefs/heads/release/v0.9.35\n"
            elif pushed and failure == "remote-missing":
                output = ""
            elif pushed and failure == "remote-error":
                code = 1
        return SimpleNamespace(returncode=code, stdout=output, stderr="")

    monkeypatch.setattr(release.subprocess, "run", run)
    with pytest.raises(release.Stop, match="checked release|remote release"):
        release.open_release_pr("v0.9.35", "patch", "intro")
    assert not any(command[:3] == ["gh", "pr", "create"] for command in calls)
    if failure in {"head-before-push", "remote-exists"}:
        assert not any("push" in command for command in calls)


@pytest.mark.parametrize("new_mode", ["100755", "120000", "160000", "000000"])
def test_release_scope_rejects_file_mode_and_type_changes(tool, monkeypatch, tmp_path, new_mode):
    release = tool("hz-release")
    hashes = {"refs/heads/main": "a" * 40, "refs/remotes/origin/main": "a" * 40, "HEAD": "b" * 40}

    def run(command, **kwargs):
        if "--abbrev-ref" in command:
            output = "release/v0.9.35\n"
        elif "status" in command:
            output = ""
        elif "rev-parse" in command:
            output = hashes[command[-1]] + "\n"
        elif "rev-list" in command:
            output = "b" * 40 + " " + "a" * 40 + "\n"
        elif "--raw" in command:
            output = f":100644 {new_mode} {'c' * 40} {'d' * 40} M\0scripts/acceptance/run_acceptance.py\0"
        elif "diff" in command:
            output = "pyproject.toml\nuv.lock\nscripts/acceptance/run_acceptance.py\n"
        else:
            pytest.fail(f"unexpected command: {command}")
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(release.subprocess, "run", run)
    monkeypatch.setattr(release, "_verify_release_lock", lambda *_: "0.9.34")
    monkeypatch.setattr(release, "_verify_release_generated_files", lambda *_: None)
    with pytest.raises(release.Stop, match="mode|type"):
        release.verify_release_pr_scope(str(tmp_path), "v0.9.35")


def test_existing_pr_and_missing_intro_do_not_acquire_gate(tool, monkeypatch, tmp_path):
    release = prepare(tool, monkeypatch, tmp_path)
    monkeypatch.setattr(release.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("unexpected subprocess"))
    monkeypatch.setattr(release, "release_pr", lambda _: {"number": 701})
    release.open_release_pr("v0.9.17", "patch", "")
    monkeypatch.setattr(release, "release_pr", lambda _: None)
    with pytest.raises(release.Stop, match="pass --intro"):
        release.open_release_pr("v0.9.17", "patch", "")


@pytest.mark.parametrize("missing", [False, True])
def test_missing_or_denied_adjacent_gate_never_falls_back(tool, monkeypatch, tmp_path, missing):
    release = prepare(tool, monkeypatch, tmp_path)
    calls = []
    gate = release.edge.gate()

    def run(command, **kwargs):
        calls.append(command)
        if command[:len(gate)] == gate:
            if missing:
                raise FileNotFoundError(gate[0])
            return SimpleNamespace(returncode=7, stdout="", stderr="gate denied")
        if "rev-parse" in command:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if "ls-remote" in command:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(release.subprocess, "run", run)
    if missing:
        with pytest.raises(FileNotFoundError):
            release.prepare_release_pr(str(tmp_path / "release-work"), "v0.9.17", "patch", "intro")
    else:
        with pytest.raises(release.Stop, match="release.sh preparation failed"):
            release.prepare_release_pr(str(tmp_path / "release-work"), "v0.9.17", "patch", "intro")
    assert sum(command[:len(gate)] == gate for command in calls) == 1
    assert not any(command[:len(gate) + 1] == [*gate, "git"] for command in calls)


def test_tag_on_main_pr_creation_stops_before_unscoped_push(tool, monkeypatch):
    release = tool("hz-release")
    monkeypatch.setattr(release, "flow_data", lambda _version: {})
    monkeypatch.setattr(release, "bump_prs", lambda _tag: [])
    monkeypatch.setattr(release, "run_release_script", lambda *_a, **_kw: pytest.fail("unscoped push attempted"))
    with pytest.raises(release.Stop, match="blocked until its --open-pr push is release-scoped"):
        release.open_bump_pr("0.9.35", "patch", "intro", False)


def test_release_lock_accepts_only_root_version_bump(tool, monkeypatch, tmp_path):
    release = tool("hz-release")
    old_lock = 'version = 1\n\n[[package]]\nname = "heinzel"\nversion = "0.9.34"\ndependencies = [{ name = "croniter" }]\n\n[[package]]\nname = "croniter"\nversion = "2.0.0"\n'
    new_lock = old_lock.replace('version = "0.9.34"', 'version = "0.9.35"', 1)
    old_project = '[project]\nname = "heinzel"\nversion = "0.9.34"\ndependencies = ["croniter"]\n\n[tool.bumpversion]\ncurrent_version = "0.9.34"\n'
    new_project = old_project.replace('version = "0.9.34"', 'version = "0.9.35"', 1).replace(
        'current_version = "0.9.34"', 'current_version = "0.9.35"', 1,
    )
    files = {
        "base:uv.lock": old_lock,
        "head:uv.lock": new_lock,
        "base:pyproject.toml": old_project,
        "head:pyproject.toml": new_project,
    }
    monkeypatch.setattr(release, "_git_show_text", lambda _work, spec: files[spec])
    assert release._verify_release_lock(str(tmp_path), "base", "head", "0.9.35") == "0.9.34"

    files["head:uv.lock"] = new_lock.replace('version = "2.0.0"', 'version = "3.0.0"')
    with pytest.raises(release.Stop, match="dependency churn"):
        release._verify_release_lock(str(tmp_path), "base", "head", "0.9.35")

    files["head:uv.lock"] = new_lock
    files["head:pyproject.toml"] = new_project.replace('dependencies = ["croniter"]', 'dependencies = ["croniter", "requests"]')
    with pytest.raises(release.Stop, match="changes beyond the release version fields"):
        release._verify_release_lock(str(tmp_path), "base", "head", "0.9.35")

    files["head:pyproject.toml"] = new_project.replace('current_version = "0.9.35"', 'current_version = "0.9.34"')
    with pytest.raises(release.Stop, match="project/bumpversion versions"):
        release._verify_release_lock(str(tmp_path), "base", "head", "0.9.35")


@pytest.mark.parametrize("legacy_rows", ["", '    **{f"0.9.{patch}": ("ui",) for patch in range(8, 16)},\n'])
def test_generated_release_files_allow_only_known_version_updates(tool, monkeypatch, tmp_path, legacy_rows):
    release = tool("hz-release")
    old_plugin = '{"name":"heinzel","version":"0.9.34","description":"cli"}\n'
    new_plugin = '{"name":"heinzel","version":"0.9.35","description":"cli"}\n'
    old_copier = 'heinzel_version:\n  type: str\n  default: "0.9.34" # bumped by the release process\n  when: false\n'
    new_copier = old_copier.replace('"0.9.34"', '"0.9.35"')
    old_acceptance = 'FOREGROUND_UI = {\n' + legacy_rows + '    "0.9.34": ("ui", "serve"),\n}\n'
    new_acceptance = old_acceptance.replace('\n}\n', '\n    "0.9.35": ("ui", "serve"),\n}\n')
    files = {
        "base:agent-skills/.claude-plugin/plugin.json": old_plugin,
        "head:agent-skills/.claude-plugin/plugin.json": new_plugin,
        "base:copier.yml": old_copier,
        "head:copier.yml": new_copier,
        "base:scripts/acceptance/run_acceptance.py": old_acceptance,
        "head:scripts/acceptance/run_acceptance.py": new_acceptance,
    }
    monkeypatch.setattr(release, "_git_show_text", lambda _work, spec: files[spec])
    release._verify_release_generated_files(str(tmp_path), "base", "head", "0.9.34", "0.9.35")

    files["head:agent-skills/.claude-plugin/plugin.json"] = new_plugin.replace("cli", "other")
    with pytest.raises(release.Stop, match="plugin metadata changed beyond"):
        release._verify_release_generated_files(str(tmp_path), "base", "head", "0.9.34", "0.9.35")
    files["head:agent-skills/.claude-plugin/plugin.json"] = new_plugin
    files["head:copier.yml"] = new_copier + "unrelated: true\n"
    with pytest.raises(release.Stop, match="copier.yml changed beyond"):
        release._verify_release_generated_files(str(tmp_path), "base", "head", "0.9.34", "0.9.35")
    files["head:copier.yml"] = new_copier
    files["head:scripts/acceptance/run_acceptance.py"] = new_acceptance + "OTHER = True\n"
    with pytest.raises(release.Stop, match="acceptance harness changed beyond"):
        release._verify_release_generated_files(str(tmp_path), "base", "head", "0.9.34", "0.9.35")


@pytest.mark.parametrize("change", [
    "new-row", "existing-row", "changed-comprehension", "changed-unpack", "changed-prior-row",
    "wrong-command", "dynamic-command", "duplicate-new-row", "duplicate-existing-row", "missing-row",
    "changed-harness",
])
def test_foreground_guard_preserves_real_table_structure(tool, monkeypatch, tmp_path, change):
    release = tool("hz-release")
    old = ('FOREGROUND_UI = {\n'
           '    **{f"0.9.{patch}": ("ui",) for patch in range(8, 16)},\n'
           '    "0.9.34": ("ui", "serve"),\n'
           '}\n\ndef foreground(version):\n    return FOREGROUND_UI[version]\n')
    row = '    "0.9.35": ("ui", "serve"),\n'
    new = old.replace('\n}\n', '\n' + row + '}\n')
    if change == "existing-row":
        old = new
    elif change == "changed-comprehension":
        new = new.replace("range(8, 16)", "range(8, 17)")
    elif change == "changed-unpack":
        new = new.replace('    **{', '    **OTHER,\n    **{')
    elif change == "changed-prior-row":
        new = new.replace('"0.9.34": ("ui", "serve")', '"0.9.34": ("ui",)')
    elif change == "wrong-command":
        new = new.replace(row, '    "0.9.35": ("ui",),\n')
    elif change == "dynamic-command":
        new = new.replace(row, '    "0.9.35": get_command(),\n')
    elif change == "duplicate-new-row":
        new = new.replace(row, row + row)
    elif change == "duplicate-existing-row":
        old = new.replace(row, row + row)
    elif change == "missing-row":
        new = old
    elif change == "changed-harness":
        new = new.replace("return FOREGROUND_UI[version]", 'return ("ui",)')
    files = {}
    for ref, version, source in (("base", "0.9.34", old), ("head", "0.9.35", new)):
        files[f"{ref}:agent-skills/.claude-plugin/plugin.json"] = '{"version":"' + version + '"}'
        files[f"{ref}:copier.yml"] = (f'heinzel_version:\n  type: str\n  default: "{version}"'
                                     ' # bumped by the release process\n  when: false\n')
        files[f"{ref}:scripts/acceptance/run_acceptance.py"] = source
    monkeypatch.setattr(release, "_git_show_text", lambda _work, spec: files[spec])
    if change in {"new-row", "existing-row"}:
        release._verify_release_generated_files(str(tmp_path), "base", "head", "0.9.34", "0.9.35")
    else:
        with pytest.raises(release.Stop, match="acceptance"):
            release._verify_release_generated_files(str(tmp_path), "base", "head", "0.9.34", "0.9.35")


def test_reviewed_release_paths_pass_scope_guard(tool, monkeypatch, tmp_path):
    release = tool("hz-release")
    hashes = {"refs/heads/main": "a" * 40, "refs/remotes/origin/main": "a" * 40, "HEAD": "b" * 40}
    changed = "".join(f":100644 100644 {'c' * 40} {'d' * 40} M\0{path}\0" for path in sorted(release.RELEASE_PR_SCOPE))

    def run(command, **kwargs):
        if "--abbrev-ref" in command:
            return SimpleNamespace(returncode=0, stdout="release/v0.9.35\n", stderr="")
        if "status" in command:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "rev-parse" in command:
            return SimpleNamespace(returncode=0, stdout=hashes[command[-1]] + "\n", stderr="")
        if "rev-list" in command:
            assert command[-1] == "b" * 40
            return SimpleNamespace(returncode=0, stdout="b" * 40 + " " + "a" * 40 + "\n", stderr="")
        if "diff" in command:
            assert command[-3:] == ["a" * 40, "b" * 40, "--"]
            return SimpleNamespace(returncode=0, stdout=changed, stderr="")
        pytest.fail(f"unexpected command: {command}")

    monkeypatch.setattr(release.subprocess, "run", run)
    monkeypatch.setattr(release, "_verify_release_lock", lambda *_: None)
    monkeypatch.setattr(release, "_verify_release_generated_files", lambda *_: None)
    assert release.verify_release_pr_scope(str(tmp_path), "v0.9.35") == "b" * 40


def test_release_scope_rejects_stale_or_divergent_parent(tool, monkeypatch, tmp_path):
    release = tool("hz-release")
    calls = []
    hashes = {"refs/heads/main": "a" * 40, "refs/remotes/origin/main": "a" * 40, "HEAD": "b" * 40}

    def run(command, **kwargs):
        calls.append(command)
        if "--abbrev-ref" in command:
            return SimpleNamespace(returncode=0, stdout="release/v0.9.35\n", stderr="")
        if "status" in command:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "rev-parse" in command:
            return SimpleNamespace(returncode=0, stdout=hashes[command[-1]] + "\n", stderr="")
        if "rev-list" in command:
            return SimpleNamespace(returncode=0, stdout="b" * 40 + " " + "c" * 40 + "\n", stderr="")
        pytest.fail("divergent ancestry must stop before the diff check")

    monkeypatch.setattr(release.subprocess, "run", run)
    with pytest.raises(release.Stop, match="not one commit directly"):
        release.verify_release_pr_scope(str(tmp_path), "v0.9.35")
    assert not any("diff" in command for command in calls)


def test_release_make_inputs_are_the_reviewed_existing_targets(tool):
    release = tool("hz-release")
    assert release.RELEASE_PR_PUSH_ENV["MAKEFLAGS"] == "WEB_TESTS= MANAGED_CHECK="
    assert release.RELEASE_PR_PUSH_ENV["FAST_TYPES"] == '"&mypy=uv run mypy" "&pyright=uv run pyright"'
    assert release.RELEASE_PR_PUSH_ENV["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "2"
    assert release.RELEASE_PR_PUSH_ENV["UNIT_TARGETS"] == " ".join((
        "tests/unit/release_summary_test.py",
        "tests/unit/release_version_test.py",
        "tests/unit/changelog_history_test.py",
        "tests/unit/release_notes_test.py",
        "tests/unit/acceptance_test.py::test_installed_metadata_selects_the_canonical_foreground_command",
        "tests/unit/acceptance_test.py::test_unrecognized_or_wrong_installed_version_never_starts_foreground",
        "tests/unit/acceptance_test.py::test_missing_installed_metadata_never_starts_foreground",
    ))
