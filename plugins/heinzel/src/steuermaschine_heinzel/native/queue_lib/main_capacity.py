"""Authenticate capacity-only dependency failures from exact workflow and job logs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import main_checkpoint
from .release_candidate import require, source_bytes, unique_object

MAX_LOG_BYTES = 256 * 1024
JOB_HEADER = re.compile(r"^  ([a-z][a-z0-9_-]*): *(?:#.*)?$", re.MULTILINE)
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z ")
ANNOTATION = re.compile(r"##\[error\]Required job ([a-z][a-z0-9_-]*) ended with (abandoned|cancelled)")
SUMMARY = "A required job ended with: "
EXIT = "##[error]Process completed with exit code 1."
NEEDS = "  NEEDS: "


@dataclass(frozen=True)
class DependencyCheck:
    step: str
    steps: tuple[str, ...]
    command: str
    names: dict[str, re.Pattern[str]]


def workflow_jobs(text: str) -> dict[str, str]:
    """Read top-level job keys and their exact bodies."""
    _, jobs_marker, jobs_text = text.partition("\njobs:\n")
    require(jobs_marker, "reviewed workflow jobs unavailable")
    headers = list(JOB_HEADER.finditer(jobs_text))
    possible_headers = re.findall(r"^  (?!#)\S[^\n]*$", jobs_text, re.MULTILINE)
    require(len(headers) == len(possible_headers), "unsupported workflow job syntax")
    blocks = {header[1]: jobs_text[header.end():headers[i + 1].start() if i + 1 < len(headers) else len(jobs_text)]
              for i, header in enumerate(headers)}
    require(len(blocks) == len(headers), "reviewed workflow has duplicate job keys")
    return blocks


def workload_step(workflow: str, job: str, matches) -> str:
    """Identify the unique unconditional named workload step in the exact workflow."""
    blocks = workflow_jobs(workflow)
    require(job in blocks, "reviewed workload job missing")
    steps = re.split(r"^      - ", blocks[job], flags=re.MULTILINE)[1:]
    hits = [step for step in steps if not re.search(r"^        if:", step, re.MULTILINE) and matches(step)]
    require(len(hits) == 1 and not re.search(r"^        continue-on-error:", hits[0], re.MULTILINE),
            "reviewed unconditional workload step is missing, ambiguous or advisory")
    label = re.match(r"name: ([^\n#]+)", hits[0])
    require(label is not None, "reviewed workload step lacks a stable display name")
    return label[1].strip()


def test_step(workflow: str) -> str:
    return workload_step(workflow, "test", lambda step: re.search(r"^        run: make test-parallel *(?:#.*)?$", step, re.MULTILINE))


def aggregate_workload_step(workflow: str) -> str:
    return workload_step(workflow, "ci-ok", lambda step: re.search(r"^          required=\(", step, re.MULTILINE)
                         and '.[$job].result == "success"' in step)


def passed_steps(job: dict, *names: str) -> None:
    require(all(sum(isinstance(step, dict) and step.get("name") == name
                    and step.get("status") == "completed" and step.get("conclusion") == "success"
                    for step in job.get("steps", [])) == 1 for name in names),
            f"required workload step did not execute successfully: {job['name']}")


def dependency_check(api, policy, head: str, *, workflow: str | None = None) -> DependencyCheck:
    """Read the owning workflow's current job labels and dependency-result step.

    This deliberately recognizes only the inline needs and named run steps
    used by the reviewed workflow. A changed layout or logger needs fresh proof.
    """
    text = source_bytes(api, policy.workflow, head, (policy.workflow,)).decode() if workflow is None else workflow
    blocks = workflow_jobs(text)
    labels = {key: re.findall(r"^    name: ([^\n#]+)", body, re.MULTILINE) for key, body in blocks.items()}
    aggregates = [key for key, names in labels.items() if [name.strip() for name in names] == [policy.aggregate]]
    require(len(aggregates) == 1, "reviewed workflow aggregate job is ambiguous")
    body = blocks[aggregates[0]]
    needs = re.findall(r"^    needs: \[([a-z0-9_, -]+)\] *(?:#.*)?$", body, re.MULTILINE)
    require(len(needs) == 1, "aggregate dependency inventory unavailable")
    dependencies = [key.strip() for key in needs[0].split(",")]
    require(len(dependencies) == len(set(dependencies)) and set(dependencies) <= blocks.keys(),
            "aggregate dependency keys are ambiguous")
    names = {}
    for key in dependencies:
        require(len(labels[key]) <= 1, "reviewed dependency has ambiguous display names")
        label = labels[key][0].strip() if labels[key] else key
        template = re.split(r"\$\{\{[^\n]*?\}\}", label)
        require(template[0], "reviewed dependency has no stable display-name prefix")
        names[key] = re.compile(".+?".join(map(re.escape, template)))

    steps = list(re.finditer(r"^      - name: ([^\n#]+?)[ ]*(?:#.*)?$", body, re.MULTILINE))
    step_names = tuple(step[1] for step in steps)
    require(steps and len(step_names) == len(set(step_names)), "reviewed aggregate step names are ambiguous")
    dependency = body[steps[0].end():steps[1].start() if len(steps) > 1 else len(body)]
    command = re.findall(r"^        run: \|\n          (.+)$", dependency, re.MULTILINE)
    require(
        len(command) == 1 and command[0].startswith("jq -r 'to_entries[] | select(")
        and 'NEEDS: ${{ toJSON(needs) }}' in dependency
        and r'Required job \(.key) ended with \(.value.result)' in dependency
        and 'echo "A required job ended with: $result"; exit 1' in dependency,
        "reviewed aggregate dependency logger is unsupported",
    )
    return DependencyCheck(steps[0][1], ("Set up job", *step_names, "Complete job"), command[0], names)


def check_aggregate(job: dict, check: DependencyCheck, cancelled: set[str], read_log) -> None:
    """Prove the only failed step named exactly the cancelled dependency groups."""
    steps = job.get("steps")
    expected = [
        (name, "failure" if name == check.step else "success" if name in ("Set up job", "Complete job") else "skipped")
        for name in check.steps
    ]
    require(isinstance(steps, list) and len(steps) == len(expected)
            and all(isinstance(step, dict) and step.get("status") == "completed"
                    and type(step.get("number")) is int and step["number"] == number
                    and (step.get("name"), step.get("conclusion")) == want
                    for number, (step, want) in enumerate(zip(steps, expected, strict=True), 1)),
            "aggregate steps do not prove a sole dependency-result failure")
    dependency_keys = set()
    for name in cancelled:
        matches = [key for key, pattern in check.names.items() if pattern.fullmatch(name)]
        require(len(matches) == 1, "cancelled job cannot be bound to one reviewed aggregate dependency")
        dependency_keys.add(matches[0])

    raw = read_log(job["id"])
    require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_LOG_BYTES, "aggregate job log missing or oversized")
    raw_lines = raw.decode().splitlines()
    lines = [TIMESTAMP.sub("", line, count=1) for line in raw_lines if TIMESTAMP.match(line)]
    run_groups = [line for line in lines if line.startswith("##[group]Run ")]
    require(run_groups == [f"##[group]Run {check.command}"], "aggregate log is missing or has an unrecognized executed step")
    errors = [line for line in lines if line.startswith("##[error]")]
    require(errors and errors[-1] == EXIT, "aggregate log has no proven dependency failure exit")
    explanations = [ANNOTATION.fullmatch(line) for line in errors[:-1]]
    require(all(explanations) and len(explanations) == len(dependency_keys)
            and {match[1] for match in explanations} == dependency_keys,
            "aggregate log does not name exactly the cancelled dependencies")
    observed = {match[1]: match[2] for match in explanations}
    require(logged_dependencies(raw_lines, check) == observed,
            "aggregate annotations disagree with the logged dependency results")
    summaries = [line.removeprefix(SUMMARY) for line in lines if line.startswith(SUMMARY)]
    require(len(summaries) == 1 and summaries[0] in observed.values()
            and all(lines.index(run_groups[0]) < lines.index(line) < lines.index(SUMMARY + summaries[0])
                    for line in errors[:-1])
            and lines.index(SUMMARY + summaries[0]) < lines.index(EXIT),
            "aggregate log has an unexplained dependency verdict")


def logged_dependencies(raw_lines: list[str], check: DependencyCheck) -> dict[str, str]:
    """Read the runner's multiline toJSON(needs) input for the failed step."""
    starts = [(index, stripped.removeprefix(NEEDS)) for index, line in enumerate(raw_lines)
              if TIMESTAMP.match(line) and (stripped := TIMESTAMP.sub("", line, count=1)).startswith(NEEDS)]
    require(len(starts) == 1, "aggregate dependency input is missing or ambiguous")
    start, first = starts[0]
    end = next((index for index in range(start + 1, len(raw_lines)) if TIMESTAMP.match(raw_lines[index])), len(raw_lines))
    require(end < len(raw_lines) and TIMESTAMP.sub("", raw_lines[end], count=1) == "##[endgroup]",
            "aggregate dependency input is not a complete runner group")
    needs = json.loads("\n".join([first, *raw_lines[start + 1:end]]), object_pairs_hook=unique_object)
    require(isinstance(needs, dict) and needs.keys() == check.names.keys()
            and all(isinstance(row, dict) and isinstance(row.get("result"), str) for row in needs.values()),
            "aggregate dependency input does not match the reviewed workflow")
    return {key: row["result"] for key, row in needs.items() if row["result"] not in ("success", "skipped")}


def read_aggregate_log(job: int) -> bytes:
    return main_checkpoint.download(f"actions/jobs/{job}/logs", MAX_LOG_BYTES, allow_escape_sequences=True)
