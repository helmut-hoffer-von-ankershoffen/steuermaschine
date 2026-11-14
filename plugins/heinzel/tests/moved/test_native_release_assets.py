import os, sys, urllib.error
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from steuermaschine_heinzel.native.release_read_lib import assets as h

TOML = '[jq]\nversion="1.8"\nurl="https://x/{version}/{file}"\n[jq.files]\ndarwin-arm64={file="jq-{version}-a",sha256="x"}\nlinux-arm64={file="n",sha256="x"}\n'

def test_tool_urls_parsed_not_hardcoded(tmp_path):
    p = tmp_path / "t.toml"; p.write_text(TOML)
    assert h.tool_urls(str(p)) == [("jq:darwin-arm64", "https://x/1.8/jq-1.8-a")]

def test_requirement_urls_missing_and_present(tmp_path):
    assert h.requirement_urls(str(tmp_path / "none")) == []
    p = tmp_path / "r.txt"; p.write_text("# c\ntools @ https://h/p/tools-1.whl\n")
    assert h.requirement_urls(str(p))[0][1] == "https://h/p/tools-1.whl"

class R:
    def __init__(s, st): s.status = st
    def __enter__(s): return s
    def __exit__(s, *a): pass
    def read(s, n): return b""

def test_probe_statuses():
    assert h.probe("https://u", opener=lambda r, timeout: R(206)) == (True, 206)
    assert h.probe("https://u", opener=lambda r, timeout: R(200))[0]
    def e504(r, timeout): raise urllib.error.HTTPError("u", 504, "x", {}, None)
    assert h.probe("https://u", opener=e504) == (False, 504)
    def to(r, timeout): raise TimeoutError()
    assert h.probe("https://u", opener=to) == (False, "TimeoutError")

def test_probe_uses_get_with_range():
    seen = []
    h.probe("https://u", opener=lambda r, timeout: (seen.append(r), R(206))[1])
    assert seen[0].get_method() == "GET" and seen[0].get_header("Range") == "bytes=0-100"

def test_digest_and_stable(tmp_path):
    p = str(tmp_path / "c.json")
    h.save([("a", "u", True, 206), ("b", "u", False, 504)], p)
    assert h.digest_line(p).startswith("ASSETS: BAD 1/2 (b)")
    h.save([("a", "u", True, 206)], p)
    assert h.digest_line(p) == "ASSETS: ok 1/1"
    seq = iter([False, True, False, True, True, True])
    ok, _ = h.wait_stable(3, 0, lambda: [("a", "u", next(seq), 200)], sleep=lambda s: None)
    assert ok
    assert h.wait_stable(2, 0, lambda: [("a", "u", False, 504)], sleep=lambda s: None, max_rounds=3)[0] is False

def test_stable_progress_reports_reset_without_changing_stdout(capsys):
    samples = iter([True, False, True, True])
    def probe_round():
        healthy = next(samples)
        return [("a", "https://example.invalid/asset", healthy, 206 if healthy else 504)]

    stable, _ = h.wait_stable(2, 0, probe_round, sleep=lambda seconds: None, max_rounds=4, report=True)
    captured = capsys.readouterr()
    assert stable
    assert not captured.out
    assert [line.rsplit(" ", 1)[-1] for line in captured.err.splitlines()] == ["1/2", "0/2", "1/2", "2/2"]
    assert "example.invalid" not in captured.err
