"""Only attested GitHub owner merge author-email metadata receives the optional CI exception."""

import importlib.util
import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_confidentiality.py"
SHA = "a" * 40
PARENT = "b" * 40
EMAIL = "fictional-marker@example.invalid"


@pytest.fixture
def checker(tmp_path, monkeypatch):
    """Entirely fictional API, identity and terms, with no network or real home access."""
    spec = importlib.util.spec_from_file_location("confidentiality_ci_fixture", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(tmp_path)
    for key in list(module.os.environ):
        if key.startswith(("GITHUB_", "CONFIDENTIAL_")):
            monkeypatch.delenv(key)
    for key, value in {
        "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path),
        "CONFIDENTIAL_TERMS": "fictional-marker", "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": "sample/project", "GITHUB_REPOSITORY_ID": "100",
        "GITHUB_REPOSITORY_OWNER_ID": "200",
    }.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "pyproject.toml").write_text('[project.urls]\nSource = "https://github.com/sample/project"\n')
    owner = {"id": 200, "login": "sample", "type": "User"}
    repository = {"id": 100, "full_name": "sample/project", "fork": False, "default_branch": "main", "owner": owner}
    commit = {
        "sha": SHA, "author": owner, "committer": {"login": "web-flow"},
        "parents": [{"sha": PARENT}],
        "commit": {
            "author": {"email": EMAIL}, "committer": {"name": "GitHub", "email": "noreply@github.com"},
            "verification": {"verified": True, "reason": "valid", "signature": "fixture-signature", "payload": "fixture-payload"},
        },
    }
    pull = {
        "number": 1, "merged": True, "state": "closed", "merged_at": "2026-01-01T00:00:00Z",
        "merge_commit_sha": SHA, "base": {"repo": repository, "ref": "main"}, "merged_by": owner,
    }
    answers = {
        "repos/sample/project": repository, f"repos/sample/project/commits/{SHA}": commit,
        f"repos/sample/project/commits/{SHA}/pulls?per_page=100": [{"number": 1, "merge_commit_sha": SHA}],
        "repos/sample/project/pulls/1": pull,
    }
    # Separate response values: mutating a PR cannot silently mutate the attested repository or owner.
    answers = deepcopy(answers)
    answers["repos/sample/project/pulls/1"] = deepcopy(pull)
    baseline = module.APPROVED_EMAIL_BASELINE
    baseline_commit = deepcopy(commit)
    baseline_commit["sha"] = baseline
    baseline_pull = deepcopy(pull)
    baseline_pull.update(number=2, merge_commit_sha=baseline)
    answers[f"repos/sample/project/commits/{baseline}"] = baseline_commit
    answers[f"repos/sample/project/commits/{baseline}/pulls?per_page=100"] = [{"number": 2, "merge_commit_sha": baseline}]
    answers["repos/sample/project/pulls/2"] = baseline_pull
    fields = ["Public author", EMAIL, "GitHub", "noreply@github.com", "fix: public body\n\nModel: fixture\n"]
    calls = []

    def git(*args):
        if args[0] == "rev-list":
            return SHA
        if args[0] == "show":
            if args[2] == "--format=%ae":
                assert args[3] == baseline
                return EMAIL
            return PARENT
        if args[0] == "log":
            if "%x00" in args[2]:
                return "\0".join(fields)
            return f"{fields[0]} <{fields[1]}>\n{fields[2]} <{fields[3]}>\n{fields[4]}"
        if args[0] == "var":
            return "Public author <user@host.local> 123 +0000"
        raise AssertionError(args)

    def api(path):
        calls.append(path)
        return answers[path]

    monkeypatch.setattr(module, "_git", git)
    monkeypatch.setattr(module, "_github_api", api)
    return module, answers, fields, calls


def scan_ci(module):
    return module.main(["--require", "--messages", "HEAD", "--github-owner-squash"])


def test_only_verified_owner_squash_author_email_is_exempt(checker):
    module, _, _, calls = checker
    assert scan_ci(module) == 0
    assert len(calls) == 7


def test_different_email_of_the_same_verified_owner_is_not_exempt(checker):
    module, answers, fields, _ = checker
    fields[1] = "other-fictional-marker@example.invalid"
    answers[f"repos/sample/project/commits/{SHA}"]["commit"]["author"]["email"] = fields[1]
    # The new email has full owner-squash provenance, but was not the approved baseline email.
    assert module.OwnerSquashProof()._eligible(SHA, fields[1])
    assert scan_ci(module) == 1


@pytest.mark.parametrize("failure", ["unsigned", "different-email", "wrong-merge", "missing-commit"])
def test_baseline_itself_requires_exact_verified_squash_proof(checker, failure):
    module, answers, _, _ = checker
    baseline = f"repos/sample/project/commits/{module.APPROVED_EMAIL_BASELINE}"
    if failure == "unsigned":
        answers[baseline]["commit"]["verification"]["verified"] = False
    elif failure == "different-email":
        answers[baseline]["commit"]["author"]["email"] = "other@example.invalid"
    elif failure == "wrong-merge":
        answers["repos/sample/project/pulls/2"]["merge_commit_sha"] = SHA
    else:
        del answers[baseline]
    assert scan_ci(module) == 1


@pytest.mark.parametrize("field", [0, 2, 3, 4])
def test_eligible_squash_still_scans_names_committer_body_and_trailers(checker, field, capsys):
    module, _, fields, _ = checker
    fields[field] += "\nPrivate-Trailer: fictional-marker"
    assert scan_ci(module) == 1
    assert "fictional-marker" not in capsys.readouterr().err


@pytest.mark.parametrize("which,path,value", [
    ("commit", ("author",), {"id": 201, "login": "other"}),
    ("commit", ("author",), None),
    ("commit", ("committer", "login"), "sample"),
    ("commit", ("commit", "author", "email"), "other@example.invalid"),
    ("commit", ("commit", "committer", "email"), "other@example.invalid"),
    ("commit", ("commit", "verification", "verified"), False),
    ("commit", ("commit", "verification", "verified"), "true"),
    ("commit", ("commit", "verification", "reason"), "bad_email"),
    ("commit", ("commit", "verification", "signature"), None),
    ("commit", ("commit", "verification", "payload"), None),
    ("commit", ("sha",), "c" * 40),
    ("commit", ("parents",), [{"sha": PARENT}, {"sha": "c" * 40}]),
    ("commit", ("parents",), [{"sha": "c" * 40}]),
    ("repo", ("fork",), True),
    ("repo", ("full_name",), "other/project"),
    ("repo", ("id",), 101),
    ("repo", ("owner", "id"), 201),
    ("pull", ("merged",), False),
    ("pull", ("merged_at",), None),
    ("pull", ("merge_commit_sha",), "c" * 40),
    ("pull", ("base", "ref"), "other"),
    ("pull", ("base", "repo", "id"), 101),
    ("pull", ("base", "repo", "full_name"), "other/project"),
    ("pull", ("merged_by",), {"id": 201, "login": "other"}),
])
def test_inconsistent_provenance_fails_closed(checker, which, path, value):
    module, answers, _, _ = checker
    target = answers[{"repo": "repos/sample/project", "commit": f"repos/sample/project/commits/{SHA}", "pull": "repos/sample/project/pulls/1"}[which]]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert scan_ci(module) == 1


def test_forged_web_flow_metadata_without_verified_signature_is_scanned(checker):
    module, answers, _, _ = checker
    answers[f"repos/sample/project/commits/{SHA}"]["commit"]["verification"] = {"verified": False, "reason": "unsigned"}
    assert scan_ci(module) == 1


@pytest.mark.parametrize("failure", [OSError("fixture"), subprocess.TimeoutExpired("gh", 30),
                                     subprocess.CalledProcessError(1, "gh", stderr="fictional-marker"),
                                     json.JSONDecodeError("fixture", "", 0)])
def test_api_errors_fail_closed_without_printing_private_response(checker, monkeypatch, capsys, failure):
    module, _, _, _ = checker

    def fail(_):
        raise failure

    monkeypatch.setattr(module, "_github_api", fail)
    assert scan_ci(module) == 1
    assert "fictional-marker" not in capsys.readouterr().err


@pytest.mark.parametrize("key,value", [("GITHUB_ACTIONS", "false"), ("GITHUB_EVENT_NAME", "pull_request"),
                                       ("GITHUB_REF", "refs/heads/feature"), ("GITHUB_REPOSITORY", "other/project"),
                                       ("GITHUB_REPOSITORY_ID", ""), ("GITHUB_REPOSITORY_OWNER_ID", "")])
def test_missing_or_ineligible_ci_context_stays_strict(checker, monkeypatch, key, value):
    module, _, _, _ = checker
    monkeypatch.setenv(key, value)
    assert scan_ci(module) == 1


def test_no_matching_email_needs_no_api_and_local_default_stays_strict(checker):
    module, _, fields, calls = checker
    assert module.main(["--require", "--messages", "HEAD"]) == 1
    assert calls == []
    fields[1] = "public@example.invalid"
    assert scan_ci(module) == 0
    assert calls == []


def test_tree_commit_message_and_hostname_checks_remain_strict(checker, tmp_path):
    module, _, fields, calls = checker
    path = tmp_path / "sample.txt"
    path.write_text("fictional-marker")
    assert module.main(["--require", str(path)]) == 1
    assert module.main(["--require", "--commit-msg", str(path)]) == 1
    assert module.main(["--require", "--identity"]) == 1
    fields[1] = "fictional-marker@host.local"
    assert scan_ci(module) == 1
    assert calls == []


def test_missing_associated_merge_is_not_proof(checker):
    module, answers, _, _ = checker
    answers[f"repos/sample/project/commits/{SHA}/pulls?per_page=100"] = []
    assert scan_ci(module) == 1


@pytest.fixture
def merge_checker(checker, monkeypatch):
    module, answers, _, _ = checker
    head = "c" * 40
    answers[f"repos/sample/project/commits/{SHA}"]["parents"] = [{"sha": PARENT}, {"sha": head}]
    pull = answers["repos/sample/project/pulls/1"]
    pull["base"]["sha"] = PARENT
    pull["head"] = {"sha": head}
    original_git = module._git

    def git(*args):
        if args == ("show", "-s", "--format=%P", SHA):
            return f"{PARENT} {head}"
        return original_git(*args)

    monkeypatch.setattr(module, "_git", git)
    return checker


def test_verified_two_parent_merge_with_exact_pr_topology_is_exempt(merge_checker):
    module, _, _, calls = merge_checker
    assert scan_ci(module) == 0
    assert len(calls) == 7


@pytest.mark.parametrize("failure", ["base", "head", "missing-head", "reversed", "root", "octopus", "local-mismatch"])
def test_merge_requires_exact_ordered_local_and_pr_parents(merge_checker, failure):
    module, answers, _, _ = merge_checker
    pull = answers["repos/sample/project/pulls/1"]
    commit = answers[f"repos/sample/project/commits/{SHA}"]
    if failure == "base":
        pull["base"]["sha"] = "d" * 40
    elif failure == "head":
        pull["head"]["sha"] = "d" * 40
    elif failure == "missing-head":
        del pull["head"]
    elif failure == "reversed":
        pull["base"]["sha"], pull["head"]["sha"] = pull["head"]["sha"], pull["base"]["sha"]
    elif failure == "root":
        commit["parents"] = []
    elif failure == "octopus":
        commit["parents"].append({"sha": "d" * 40})
    else:
        commit["parents"].reverse()
    assert scan_ci(module) == 1


@pytest.mark.parametrize("field", [0, 2, 3, 4])
def test_eligible_merge_still_scans_other_fields(merge_checker, field, capsys):
    module, _, fields, _ = merge_checker
    fields[field] += "\nPrivate-Trailer: fictional-marker"
    assert scan_ci(module) == 1
    assert "fictional-marker" not in capsys.readouterr().err


def test_verified_merge_still_requires_unchanged_baseline_email(merge_checker):
    module, answers, fields, _ = merge_checker
    fields[1] = "other-fictional-marker@example.invalid"
    answers[f"repos/sample/project/commits/{SHA}"]["commit"]["author"]["email"] = fields[1]
    assert module.OwnerSquashProof()._eligible(SHA, fields[1])
    assert scan_ci(module) == 1


def test_baseline_remains_single_parent_squash_only(checker, monkeypatch):
    module, answers, _, _ = checker
    baseline = module.APPROVED_EMAIL_BASELINE
    head = "c" * 40
    answers[f"repos/sample/project/commits/{baseline}"]["parents"] = [{"sha": PARENT}, {"sha": head}]
    pull = answers["repos/sample/project/pulls/2"]
    pull["base"]["sha"] = PARENT
    pull["head"] = {"sha": head}
    original_git = module._git

    def git(*args):
        if args == ("show", "-s", "--format=%P", baseline):
            return f"{PARENT} {head}"
        return original_git(*args)

    monkeypatch.setattr(module, "_git", git)
    assert scan_ci(module) == 1
