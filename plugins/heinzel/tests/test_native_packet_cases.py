"""`sm head cases collect|compare` and the moved case collector. The collection is real (pytest --collect-only on a
tree extracted from a scratch repository; a stand-in vitest for the web lane) and runs under the real run lock in a
sandbox HOME. The recorded inventories are those of fixtures/packet/recorded (copied unchanged 2026-11-12)."""

import json
import subprocess
import sys
from pathlib import Path

import packet_parity as parity
import pytest
from test_native_packet_dirs import assert_collections_recorded, gated_requests, rt, runtime_for  # noqa: F401  (fixtures)
from test_native_packet_wave import _git, _write, wave  # noqa: F401  (wave is a fixture)

from steuermaschine.testing import invoke
from steuermaschine_heinzel import native
from steuermaschine_heinzel.native.packet_lib import check_ops
from steuermaschine_heinzel.native.packet_lib import collect_cases as cc
from steuermaschine_heinzel.native.packet_lib import focused_manifest as fm

VITEST = '''#!/usr/bin/env python3
import sys
out = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--outputFile.junit="))
files = [a for a in sys.argv[2:] if not a.startswith("--")] or ["src/a.test.ts", "src/b.test.ts"]
body = "".join(f'<testcase classname="{f}" name="suite > case"/>' for f in files)
open(out, "w").write(f"<testsuites><testsuite>{body}</testsuite></testsuites>")
'''


def junit(path, pairs):
    path.write_text("<testsuites><testsuite>" + "".join(f'<testcase classname="{c}" name="{n}"/>' for c, n in pairs) + "</testsuite></testsuites>")
    return path


# ----------------------------------------------------------------------------- the moved collector, in process
def test_node_ids_become_the_junit_names_pytest_reports():
    assert cc.mangle("tests/unit/a_test.py::test_one") == ("tests.unit.a_test", "test_one")
    assert cc.mangle("tests/unit/a_test.py::TestX::test_two[a/b-1]") == ("tests.unit.a_test.TestX", "test_two[a/b-1]")
    assert cc.pytest_cases("tests/a_test.py::test_x\n\n2 tests collected in 0.01s\n<Module x>\n") == [("tests.a_test", "test_x")]
    for bad in ("no tests ran\n", "README.md::x\n"):
        with pytest.raises(cc.CollectError):
            cc.pytest_cases(bad)


def test_the_collector_digest_is_the_digest_a_packet_binds(tmp_path):
    cases = [("b.mod", "test_2"), ("a.mod", "test_1")]
    path = tmp_path / "cases.xml"
    cc.write_junit(cases, path)
    assert cc.read_junit(path) == sorted(cases)
    assert cc.digest(cases) == fm.cases_digest(fm.junit_cases(path.read_bytes()))
    with pytest.raises(cc.CollectError, match="duplicate"):
        cc.write_junit([*cases, cases[0]], path)


@pytest.mark.parametrize("name", parity.ALL)
def test_recorded_inventories_give_the_recorded_case_digests(name, tmp_path):
    directory = parity.RECORDED / name
    packet = json.loads((directory / "packet.json").read_text())
    for lane, digest in packet["manifest"]["case_sha256"].items():
        cases = cc.read_junit(directory / f"{lane}.main-collected.xml")
        assert cc.digest(cases) == digest == fm.cases_digest(fm.junit_cases((directory / f"{lane}.main-collected.xml").read_bytes()))
        rewritten = tmp_path / f"{lane}.xml"
        cc.write_junit(cases, rewritten)  # the collector's own file layout (the recorded files keep collection order)
        assert cc.read_junit(rewritten) == cases and rewritten.read_bytes()[:60] == (directory / f"{lane}.main-collected.xml").read_bytes()[:60]
        if (directory / f"{lane}.pr-collected.xml").exists():
            compared = check_ops.compare(directory / f"{lane}.pr-collected.xml", directory / f"{lane}.pr-collected.xml", directory / f"{lane}.main-collected.xml")
            assert compared.status == "identical" and (directory / f"{lane}.case-diff.txt").read_text() == ""


def test_compare_is_the_admission_rule(tmp_path, capsys):
    run = junit(tmp_path / "run.xml", [("m", "a"), ("m", "b")])
    same = junit(tmp_path / "same.xml", [("m", "b"), ("m", "a")])
    more = junit(tmp_path / "more.xml", [("m", "a"), ("m", "b"), ("m", "scan[x]")])
    assert cc.compare(cc.read_junit(run), cc.read_junit(same), cc.read_junit(same)) == (0, [])
    assert cc.compare(cc.read_junit(run), cc.read_junit(same), cc.read_junit(more)) == (3, ["+ resulting-tree-only m::scan[x]"])
    assert cc.compare(cc.read_junit(more), cc.read_junit(same), cc.read_junit(same)) == (1, ["- hosted-only m::scan[x]"])
    assert cc.compare(cc.read_junit(run), cc.read_junit(more), cc.read_junit(more))[1] == ["+ collected-only m::scan[x]"]
    assert cc.compare(cc.read_junit(more), cc.read_junit(more), cc.read_junit(run))[1] == ["- pr-tree-only m::scan[x]"]
    diff = tmp_path / "diff.txt"
    assert cc.main(["compare", "--hosted", str(run), "--pr", str(same), "--result", str(more), "-o", str(diff)]) == 3
    assert diff.read_text() == "+ resulting-tree-only m::scan[x]\n" and "resulting-tree-only" in capsys.readouterr().err
    assert cc.main(["compare", "--hosted", str(run), "--pr", str(same), "--result", str(tmp_path / "absent.xml")]) == 1
    assert "REFUSED" in capsys.readouterr().err


def test_collect_lists_the_cases_of_an_exact_tree_without_running_them(wave, capsys):  # noqa: F811
    sel, out, work = wave["sel-a"], wave["tmp"] / "collected.xml", wave["tmp"] / "work"
    tree = _git(wave["repo"], "rev-parse", f"{wave['a']}^{{tree}}")
    assert cc.main(["collect", "--repo", str(wave["repo"]), "--tree", tree, "--selection", str(sel), "--lane", "python", "--kind", "pytest", "-o", str(out), "--work", str(work)]) == 0
    assert cc.read_junit(out) == [("tests.unit.a_test", "test_one"), ("tests.unit.a_test", "test_two")]
    assert f"python {tree[:12]} 2 cases digest {cc.digest(cc.read_junit(out))}" in capsys.readouterr().err
    assert list(work.iterdir()) == []  # the extracted tree is removed
    base = _git(wave["repo"], "rev-parse", f"{wave['main']}^{{tree}}")
    assert cc.collect(wave["repo"], base, {"python": ["tests/unit/a_test.py"]}, "python", sys.executable, None, work, "pytest") == [("tests.unit.a_test", "test_one")]
    for lane, kind, selection, message in (("fuzz", "managed", {"fuzz": ["x"]}, "unknown lane kind managed for lane fuzz"), ("python", "pytest", {}, "has no selection"),
                                           ("web", "vitest", {"web": ["web/a.test.ts"]}, "--node-modules is required"),
                                           ("python", "pytest", {"python": ["tests/unit/absent_test.py"]}, "collection failed")):
        with pytest.raises(cc.CollectError, match=message):
            cc.collect(wave["repo"], base, selection, lane, sys.executable, None, work, kind)
    with pytest.raises(SystemExit):  # the kind is required: the caller reads it from Core's release policy
        cc.main(["collect", "--repo", str(wave["repo"]), "--tree", base, "--selection", str(sel), "--lane", "python", "-o", str(out), "--work", str(work)])


def test_the_web_lane_runs_the_selected_vitest_files_or_the_whole_suite(wave):  # noqa: F811
    modules = wave["tmp"] / "node_modules"
    (modules / ".bin").mkdir(parents=True)
    (modules / ".bin/vitest").write_text(VITEST)
    (modules / ".bin/vitest").chmod(0o755)
    _write(wave["repo"], {"web/src/a.test.ts": "", "web/src/b.test.ts": ""})
    _git(wave["repo"], "add", "-A")
    _git(wave["repo"], "commit", "-qm", "web")
    tree, work = _git(wave["repo"], "rev-parse", "HEAD^{tree}"), wave["tmp"] / "work"
    work.mkdir()
    assert cc.collect(wave["repo"], tree, {"web": ["web/src/a.test.ts"]}, "web", sys.executable, modules, work, "vitest") == [("src/a.test.ts", "suite > case")]
    whole = cc.collect(wave["repo"], tree, {"web": ["web/ALL"]}, "web", sys.executable, modules, work, "vitest")
    assert whole == [("src/a.test.ts", "suite > case"), ("src/b.test.ts", "suite > case")]  # the sentinel: no file filter


def test_the_collector_file_runs_as_a_real_isolated_process(tmp_path):
    run = junit(tmp_path / "run.xml", [("m", "a")])
    done = subprocess.run([sys.executable, "-I", cc.__file__, "compare", "--hosted", str(run), "--pr", str(run), "--result", str(run)], capture_output=True, text=True)
    assert done.returncode == 0 and done.stdout == ""
    assert "from ." not in Path(cc.__file__).read_text() and "steuermaschine" not in Path(cc.__file__).read_text()  # it must load without the package


# ----------------------------------------------------------------------------- the commands
def test_cases_collect_in_text_and_json_under_the_run_lock(rt, wave, gated_requests):  # noqa: F811
    out = wave["tmp"] / "python.collected.xml"
    argv = ["head", "cases", "collect", "--repo", str(wave["repo"]), "--tree", wave["a"], "--selection", str(wave["sel-a"]), "--lane", "python", "--output", str(out)]
    done = invoke(rt, argv)
    assert done.exit_code == 0, done
    digest = cc.digest(cc.read_junit(out))
    assert done.stdout.splitlines() == [f"python {wave['a'][:12]} 2 cases digest {digest}", f"wrote {out}"]
    assert out.read_bytes() == wave["junit-a"].read_bytes() or cc.read_junit(out) == cc.read_junit(wave["junit-a"])
    collected = check_ops.CasesCollected.model_validate(json.loads(invoke(rt, ["--format", "json", *argv]).stdout))
    assert collected.cases == 2 and collected.digest == digest and collected.lane == "python" and collected.tree == wave["a"] and collected.output == str(out)
    history = [json.loads(line) for line in (wave["tmp"] / "home/history.jsonl").read_text().splitlines()]
    assert_collections_recorded(history, gated_requests)
    assert not list(Path(out).parent.glob("cases-collect-*"))


def test_cases_collect_refuses_when_the_collection_fails(rt, wave):  # noqa: F811
    out = wave["tmp"] / "none.xml"
    done = invoke(rt, ["head", "cases", "collect", "--repo", str(wave["repo"]), "--tree", wave["a"], "--selection", str(wave["sel-a"]), "--lane", "fuzz", "-o", str(out)])
    assert done.exit_code == 1 and "collecting fuzz on" in done.stderr and "not a focused lane of Core's release policy" in done.stderr and done.stdout == "" and not out.exists()
    done = invoke(rt, ["head", "cases", "collect", "--repo", str(wave["repo"]), "--tree", wave["a"], "--selection", str(wave["sel-a"]), "--lane", "managed", "-o", str(out)])
    assert done.exit_code == 1 and "kind managed, which the case collector cannot list" in done.stderr and not out.exists()
    done = invoke(runtime_for(wave, wave["repo"]), ["head", "cases", "collect", "--tree", "0" * 40, "--selection", str(wave["sel-a"]), "--lane", "python", "-o", str(out)])
    assert done.exit_code == 1 and f"collecting python on {'0' * 12}: no Core tree" in done.stderr  # --repo defaults to the setting core_repo_dir


def test_a_command_without_a_core_repository_refuses_and_names_the_setting(rt):  # noqa: F811
    from steuermaschine.engine import Refuse
    from steuermaschine.native import NativeRuntime, using
    from steuermaschine_heinzel.native import packet

    with using(NativeRuntime(rt.providers, {}, None)), pytest.raises(Refuse, match="core_repo_dir") as refused:
        packet.head_size(["a"], None, "origin/main")
    assert refused.value.code == 2


def test_cases_compare_in_text_and_json_with_its_exit_codes(rt, tmp_path):  # noqa: F811
    run = junit(tmp_path / "run.xml", [("m", "a"), ("m", "b")])
    more = junit(tmp_path / "more.xml", [("m", "a"), ("m", "b"), ("m", "scan[x]")])
    argv = ["head", "cases", "compare", "--hosted", str(run), "--pr", str(run)]
    done = invoke(rt, [*argv, "--result", str(run)])
    assert done.exit_code == 0 and done.stdout.startswith("identical: ")
    done = invoke(rt, [*argv, "--result", str(more), "--output", str(tmp_path / "diff.txt")])
    assert done.exit_code == 3 and done.stdout.splitlines()[0].startswith("TREE-SCANNING: the resulting tree differs in 1 id(s)")
    assert done.stdout.splitlines()[1] == "+ resulting-tree-only m::scan[x]" == (tmp_path / "diff.txt").read_text().strip()
    scanning = invoke(rt, ["--format", "json", *argv, "--result", str(more)])
    compared = check_ops.CasesCompared.model_validate(json.loads(scanning.stdout))
    assert scanning.exit_code == 3 and compared.status == "tree_scanning" and compared.lines == ["+ resulting-tree-only m::scan[x]"] and compared.output == ""
    done = invoke(rt, ["head", "cases", "compare", "--hosted", str(more), "--pr", str(run), "--result", str(run)])
    assert done.exit_code == 1 and done.stdout.splitlines() == ["REFUSED: the head-tree collection does not reproduce the run (1 id(s) differ)", "- hosted-only m::scan[x]"]
    done = invoke(rt, [*argv, "--result", str(tmp_path / "absent.xml")])
    assert done.exit_code == 1 and "No such file" in done.stderr and done.stdout == ""


def test_registrations():
    reg = {c.path: c for c in native.commands() if hasattr(c, "writes")}
    assert reg["head cases collect"].writes is True and reg["head cases compare"].writes is True
