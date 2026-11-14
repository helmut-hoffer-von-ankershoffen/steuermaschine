# ruff: noqa: F841
"""The advisory classifier library, moved (ci_classify_lib/jev.py). These are the assertions of the old library's test file;
only the seam changed: the configuration is CONFIG (filled from the settings) instead of environment variables, and
the keychain is read through the injected process runner. The fake service listens on the loopback interface."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from steuermaschine_heinzel.native.ci_classify_lib import jev as j

from steuermaschine.protocols import Result

KEY = "apikey_" + "ab12cd34" * 4 + "_s3cr3tS3cr3tVALUE"


class Fake:
    """Local server: replies from a scripted list of (status, body, headers); records requests."""
    def __init__(self, script):
        self.script, self.seen = list(script), []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                outer.seen.append({"auth": self.headers.get("Authorization"), "body": json.loads(self.rfile.read(n) or b"{}")})
                st, body, hdr = outer.script.pop(0) if outer.script else (200, OK, {})
                self.send_response(st)
                for k, v in hdr.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def log_message(self, *a):
                pass
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


OK = {"model": "jev-1.14.0", "answers": {"cause": {"choice": "timeout", "confidence": 0.95, "probabilities": {"timeout": 0.95, "infra-flake": 0.05}}}, "usage": {"input_tokens": 10}}
REPO = j.ALLOWED_REPOS[0]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(j, "CONFIG", {"dir": str(tmp_path), "mode": "on", "api_key": KEY, "url": "http://127.0.0.1:1"})  # tests never reach the real API
    monkeypatch.setattr(j, "_sleep", lambda s: None)
    return tmp_path


def serve(monkeypatch, script):
    f = Fake(script)
    monkeypatch.setitem(j.CONFIG, "url", f.url)
    return f


Q = [j.choice("cause", "why", dict(j.CLASSES))]


def test_success_bearer_and_typed_answer(env, monkeypatch, capsys):
    f = serve(monkeypatch, [])
    r = j.ask("log", Q, repo=REPO, tool="t")
    assert j.confident(j.answer(r, "cause")) == "timeout"
    assert f.seen[0]["auth"] == f"Bearer {KEY}" and f.seen[0]["body"]["model"] == "jev-1.14.0"
    assert j.jev_line("log", REPO) == "jev: timeout p=0.95"
    assert KEY not in capsys.readouterr().err


def test_default_off_and_kill_switch_send_nothing(env, monkeypatch):
    f = serve(monkeypatch, [])
    monkeypatch.delitem(j.CONFIG, "mode")
    assert j.ask("x", Q, repo=REPO) is None
    monkeypatch.setitem(j.CONFIG, "mode", "off")
    assert j.ask("x", Q, repo=REPO) is None and not j.available()
    monkeypatch.setitem(j.CONFIG, "mode", "on")
    (env / "OFF").write_text("")
    assert j.ask("x", Q, repo=REPO) is None and j.killed()
    assert f.seen == []


def test_no_key_no_request(env, monkeypatch):
    f = serve(monkeypatch, [])
    monkeypatch.delitem(j.CONFIG, "api_key")
    assert j.key() == "" and j.ask("x", Q, repo=REPO) is None and f.seen == []  # no keychain item configured: nothing is run
    asked = []
    monkeypatch.setitem(j.CONFIG, "keychain_item", "item")
    monkeypatch.setitem(j.CONFIG, "run", lambda argv, timeout=None: asked.append(argv) or Result(44, "", "not found"))
    assert j.ask("x", Q, repo=REPO) is None and f.seen == [] and asked == [["security", "find-generic-password", "-s", "item", "-w"]]
    monkeypatch.setitem(j.CONFIG, "run", lambda argv, timeout=None: Result(0, KEY + "\n", ""))
    assert j.key() == KEY and j.available()


def test_configured_key_never_shells_out(env, monkeypatch):
    monkeypatch.setitem(j.CONFIG, "keychain_item", "item")
    monkeypatch.setitem(j.CONFIG, "run", lambda *a, **k: pytest.fail("shelled out"))
    assert j.key() == KEY


def test_repo_allowlist(env, monkeypatch):
    f = serve(monkeypatch, [])
    for bad in ("helmut-hoffer-von-ankershoffen/heinzel-management", "someone/else", "", None):
        assert j.ask("x", Q, repo=bad) is None and j.jev_line("x", bad) is None
    assert f.seen == []
    assert j.ask("x", Q, repo=j.ALLOWED_REPOS[1]) is not None


def test_retry_after_then_success(env, monkeypatch):
    waits = []
    monkeypatch.setattr(j, "_sleep", waits.append)
    f = serve(monkeypatch, [(429, {}, {"Retry-After": "2"}), (200, OK, {})])
    assert j.ask("x", Q, repo=REPO) is not None and len(f.seen) == 2 and waits == [2.0]


def test_server_errors_fall_back_to_none(env, monkeypatch, capsys):
    f = serve(monkeypatch, [(500, {}, {})] * 3)
    assert j.ask("x", Q, repo=REPO) is None and len(f.seen) == 3  # 1 + 2 retries
    f = serve(monkeypatch, [(422, {}, {})])
    assert j.ask("x", Q, repo=REPO) is None and len(f.seen) == 1  # not retried
    f = serve(monkeypatch, [(200, "not a dict", {})])
    assert j.ask("x", Q, repo=REPO) is None
    monkeypatch.setitem(j.CONFIG, "url", "http://127.0.0.1:1")  # connection refused
    assert j.ask("x", Q, repo=REPO) is None and j.jev_line("x", REPO) is None
    assert KEY not in capsys.readouterr().err


def test_401_disables_for_an_hour(env, monkeypatch):
    f = serve(monkeypatch, [(401, {}, {})])
    assert j.ask("x", Q, repo=REPO) is None
    assert j.ask("x", Q, repo=REPO) is None and len(f.seen) == 1 and not j.available()


def test_oversized_state_skipped(env, monkeypatch):
    f = serve(monkeypatch, [])
    assert j.ask("a" * (j.MAX_STATE_CHARS + 1), Q, repo=REPO) is None and f.seen == []


def test_budget_cap_and_count(env, monkeypatch):
    f = serve(monkeypatch, [])
    monkeypatch.setattr(j, "DAILY_CALLS", 2)
    assert j.ask("x", Q, repo=REPO) and j.ask("x", Q, repo=REPO)
    assert j.ask("x", Q, repo=REPO) is None and len(f.seen) == 2
    assert j.budget_use()["calls"] == 2
    monkeypatch.setattr(j, "DAILY_CALLS", 99)
    monkeypatch.setattr(j, "DAILY_TOKENS", 1)
    assert j.ask("x" * 100, Q, repo=REPO) is None


def test_low_confidence_gives_no_label(env, monkeypatch):
    low = dict(OK, answers={"cause": {"choice": "timeout", "confidence": 0.5}})
    serve(monkeypatch, [(200, low, {})])
    assert j.jev_line("x", REPO) is None
    assert j.confident({"noul": 0.95}) is True and j.confident({"noul": 0.5}) is None and j.confident({"noul": 0.05}) is False


def test_redact_and_key_never_in_exception_or_output(env, monkeypatch, capsys):
    assert KEY not in j.redact(f"boom {KEY} end") and "apikey_ab12cd34..." in j.redact(KEY)
    assert KEY not in j.redact(f"x {KEY[7:]}", KEY)
    boom = serve(monkeypatch, [])
    monkeypatch.setattr(j, "_http", lambda *a, **k: (_ for _ in ()).throw(RuntimeError(f"leak {KEY}")))
    assert j.ask("x", Q, repo=REPO) is None
    err = capsys.readouterr().err
    assert KEY not in err and "apikey_ab12cd34..." in err
    assert KEY not in capsys.readouterr().out


def test_base_url_refuses_remote_http(env, monkeypatch):
    monkeypatch.setitem(j.CONFIG, "url", "http://evil.example")
    assert j._base_url() == j.BASE


def test_source_has_no_key_literal():
    assert not j.KEY_RE.search(Path(j.__file__).read_text())


def test_configure_replaces_the_configuration_and_the_default_state_directory(monkeypatch):
    monkeypatch.setattr(j, "CONFIG", {"mode": "on"})
    j.configure(mode="off", dir="")
    assert j.CONFIG == {"mode": "off", "dir": ""} and j.state_dir().endswith("/.cache/jev") and not j.wanted()
    assert j.budget_use()["calls"] == 0 or isinstance(j.budget_use()["calls"], int)
    assert j.noul("n", "i") == {"name": "n", "type": "noul", "instructions": "i"} and j.score("s", "i", [1])["levels"] == [1]
    assert j.answer({"answers": [{"name": "a", "x": 1}, "junk"]}, "a") == {"name": "a", "x": 1} and j.answer(None, "a") is None and j.confident("no") is None


def test_the_moved_file_is_the_original_after_the_listed_rules():
    """The verifier of the move; skipped where the old tools directory does not exist."""
    import ci_verbatim

    tools = Path("~/Code/heinzel-management/tools").expanduser()
    if not (tools / "hz_jev.py").exists():
        pytest.skip("the old tools directory is not on this machine")
    assert ci_verbatim.expected(tools) == ci_verbatim.MOVED.read_text()


def test_the_triage_definitions_are_the_originals_after_the_listed_rules():
    import ci_verbatim

    tools = Path("~/Code/heinzel-management/tools").expanduser()
    if not (tools / "hz_triage.py").exists():
        pytest.skip("the old tools directory is not on this machine")
    assert ci_verbatim.missing_extracts(tools) == []
