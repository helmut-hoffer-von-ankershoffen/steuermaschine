"""Recognize the protected direct-full selection contract; unknown shapes refuse.

This is source admission, not candidate proof. Inventories and their conclusions
remain the existing release candidate verifier's responsibility.
"""

from __future__ import annotations

import ast
import inspect
import re
from collections.abc import Hashable
from typing import Any, cast

import yaml

from steuermaschine.engine import Refuse

from ..native.queue_lib import core_policy, main_batch
from ..native.release_admit_lib import release_scope


def require(condition: object, message: str) -> None:
    if not condition:
        raise Refuse(f"protected direct-full contract: {message}")


class WorkflowLoader(yaml.BaseLoader):
    """No aliases or duplicate keys: neither can hide a second selector."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        require(not self.check_event(yaml.AliasEvent), "workflow aliases are unsupported")
        node = super().compose_node(parent, index)
        require(node is not None, "empty workflow node")
        assert node is not None
        require(node.tag in {"tag:yaml.org,2002:str", "tag:yaml.org,2002:seq", "tag:yaml.org,2002:map"},
                "explicit workflow tags are unsupported")
        return node

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Hashable, Any]:
        result: dict[Hashable, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            require(isinstance(key, str) and key not in result and key != "<<", "duplicate or merged workflow key")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _producer(job: dict[str, Any], output: str) -> dict[str, Any]:
    expression = job.get("outputs", {}).get(output, "")
    match = re.fullmatch(r"\$\{\{ steps\.([\w-]+)\.outputs\.([\w-]+) }}", expression)
    require(match is not None, f"{output} is not bound to a step output")
    assert match is not None
    steps = [step for step in job.get("steps", []) if step.get("id") == match[1]]
    require(len(steps) == 1, f"{output} has no unique producer")
    require(match[2] == ("selected" if output == "full_ci" else output), f"{output} producer output differs")
    return cast("dict[str, Any]", steps[0])


def _batch(source: bytes, policy: core_policy.Policy) -> None:
    # Compare the complete executable AST, including authority helpers, constants,
    # imports and the output writer. Only known native-port substitutions differ.
    # This is the existing shared batch protocol, not execution of candidate code.
    actual = _WithoutDocs().visit(_MainAdapter().visit(ast.parse(source)))
    expected = ast.parse(inspect.getsource(main_batch))
    expected.body = [node for node in expected.body if not isinstance(node, ast.ImportFrom) or not node.level]
    expected.body.extend(ast.parse('if __name__ == "__main__":\n    main()\n').body)
    expected = _WithoutDocs().visit(_FirstAttemptBatch(policy.workflow).visit(expected))
    require(ast.dump(actual) == ast.dump(expected), "unknown batch selection or authentication semantics")


class _WithoutDocs(ast.NodeTransformer):
    def visit_Expr(self, node: ast.Expr) -> ast.AST | None:
        return None if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) else self.generic_visit(node)


class _FirstAttemptBatch(ast.NodeTransformer):
    """Specialize the reviewed native batch protocol to its default first-attempt path."""

    def __init__(self, workflow: str):
        self.workflow = workflow

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        for index, argument in reversed(list(enumerate(node.args.kwonlyargs))):
            if argument.arg == "reruns":
                default = node.args.kw_defaults[index]
                require(isinstance(default, ast.Constant) and default.value is False, "native batch default changed")
                del node.args.kwonlyargs[index]
                del node.args.kw_defaults[index]
        return self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> ast.AST:
        if ast.unparse(node.func) == "core_facts.ci_path" and not node.args and not node.keywords:
            return ast.Constant(self.workflow)
        node.keywords = [keyword for keyword in node.keywords
                         if not (keyword.arg == "reruns" and isinstance(keyword.value, ast.Name) and keyword.value.id == "reruns")]
        return self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> ast.AST:
        if isinstance(node.test, ast.Name) and node.test.id == "reruns":
            return cast("ast.AST", self.visit(node.orelse))
        return self.generic_visit(node)


class _MainAdapter(ast.NodeTransformer):
    """Normalize only the existing native port's transport substitutions, never its conditions."""

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        core = any(isinstance(child, ast.Attribute) and ast.unparse(child) == "os.environ" for child in ast.walk(node))
        self.generic_visit(node)
        if node.name == "main" and core:
            node.body.insert(2, ast.parse("actions = environment.actions()").body[0])
        return node

    def visit_Call(self, node: ast.Call) -> ast.AST:
        if ast.unparse(node.func) == "os.environ.get" and len(node.args) == 1 and not node.keywords:
            key = ast.literal_eval(node.args[0])
            field = {"GITHUB_REPOSITORY": "repository", "GITHUB_EVENT_NAME": "event_name",
                     "GITHUB_REF": "ref", "GITHUB_RUN_ATTEMPT": "run_attempt"}.get(key)
            require(field is not None, "unknown batch environment selector")
            return ast.parse(f"actions.{field}", mode="eval").body
        if ast.unparse(node.func) == "subprocess.run":
            node.func = ast.parse("edge.run", mode="eval").body
        return self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> ast.AST:
        if ast.unparse(node.value) == "os.environ":
            field = {"GITHUB_SHA": "sha", "GITHUB_OUTPUT": "output"}.get(ast.literal_eval(node.slice))
            require(field is not None, "unknown batch environment input")
            return ast.parse(f"str(actions.{field})", mode="eval").body
        return self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        original = ast.unparse(node.left)
        self.generic_visit(node)
        if original == "os.environ.get('GITHUB_RUN_ATTEMPT')" and len(node.comparators) == 1 and isinstance(node.comparators[0], ast.Constant):
            require(node.comparators[0].value == "1", "batch rerun admission changed")
            node.comparators[0].value = 1
        return node


def _workflow(raw: bytes, policy: core_policy.Policy) -> str:
    doc = yaml.load(raw, Loader=WorkflowLoader)
    require(isinstance(doc, dict) and isinstance(doc.get("jobs"), dict), "workflow has no jobs")
    events = doc.get("on", {})
    require(isinstance(events, dict) and events.get("push", {}).get("branches") == ["main"]
            and "pull_request" in events, "workflow does not run main pushes and PRs")
    jobs = doc["jobs"]
    producers = [(key, job) for key, job in jobs.items() if isinstance(job, dict) and "full_ci" in job.get("outputs", {})]
    require(len(producers) == 1, "no unique full-workload selector")
    selector_id, selector = producers[0]
    outputs = selector["outputs"]
    require(outputs.get("focused_mode") == outputs.get("focused_base") == ""
            and json_matrix(outputs.get("focused_matrix")), "focused selection remains enabled")
    step = _producer(selector, "full_ci")
    require(step.get("if") == "github.event_name != 'schedule'"
            and step.get("run") == 'echo \'selected=true\' >> "$GITHUB_OUTPUT"'
            and not set(step) - {"id", "name", "if", "run"}, "full selector is conditional or has extra execution controls")
    admission_id = selector.get("needs")
    require(isinstance(admission_id, str) and admission_id in jobs, "selector has unknown admission dependencies")
    require(selector.get("if") == f"github.event_name != 'push' || needs.{admission_id}.outputs.full != 'false'",
            "main selection has an unknown condition")
    admission = jobs[admission_id]
    require(not any(key in admission for key in ("if", "needs", "continue-on-error", "uses")), "batch producer is conditional")
    batch = _producer(admission, "full")
    require(not set(batch) - {"id", "name", "env", "run"}, "batch step has unknown controls")
    match = re.fullmatch(r"uv run --no-build --script ([\w./-]+\.py)", batch.get("run", ""))
    require(match is not None and core_policy.repo_path(match[1]), "unknown batch invocation")
    require(not any(key in selector for key in ("continue-on-error", "uses")), "selector has unknown execution controls")
    # A policy beside an empty/partial workflow is not a contract. All declared
    # successful jobs must have a runnable definition (matrix names are templates).
    for name, verdict in policy.expected_jobs("pull_request").items():
        if verdict != "success":
            continue
        require(any(_job_name(job.get("name", ""), name) and (job.get("steps") or job.get("uses"))
                    for job in jobs.values() if isinstance(job, dict)), f"required job has no runnable definition: {name}")
    # Workload jobs must consume the selected full output, not a disconnected marker.
    require(any(f"needs.{selector_id}.outputs.full_ci" in str(job.get("if", ""))
                for job in jobs.values() if isinstance(job, dict)), "full selector has no workload consumers")
    assert match is not None
    return match[1]


def json_matrix(value: object) -> bool:
    import json

    try:
        return isinstance(value, str) and json.loads(value) == {"include": []}
    except ValueError:
        return False


def _job_name(template: str, name: str) -> bool:
    parts = re.split(r"(\$\{\{ matrix\.[\w-]+ }})", template)
    pattern = "".join(".+" if part.startswith("${{") else re.escape(part) for part in parts)
    return re.fullmatch(pattern, name) is not None


def verify(clone: str, base: str, head: str, version: str) -> None:
    """Read protected and prospective immutable objects; never infer full mode from a reader failure."""
    try:
        policy = core_policy.at_commit_git(clone, base)
        paths = {core_policy.PATH, policy.workflow, policy.recorder, policy.judge_workflow}
        protected = core_policy.blobs_at(clone, base, paths)
        batch = _workflow(protected[policy.workflow], policy)
        protected.update(core_policy.blobs_at(clone, base, {batch}))
        _batch(protected[batch], policy)
        prospective = core_policy.blobs_at(clone, head, set(protected))
        require(prospective == protected, "release changes protected execution or selection sources")
        # Reuse the generated-release validator: every other file, including all
        # transitive execution inputs, must be unchanged. Even the permitted
        # lock/config/acceptance changes are restricted to their generated rows.
        metadata = {side: core_policy.blobs_at(clone, commit, set(release_scope.SCOPE))
                    for side, commit in (("base", base), ("head", head))}
        raw = core_policy.git_read(clone, "diff", "--raw", "--no-abbrev", "--no-renames", "-z", base, head, "--")
        release_scope.validate(lambda side, path: metadata[side][path], raw, version)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, SyntaxError, yaml.YAMLError, core_policy.CandidateError) as exc:
        raise Refuse(f"protected direct-full source is unreadable or unsupported: {exc}") from None
