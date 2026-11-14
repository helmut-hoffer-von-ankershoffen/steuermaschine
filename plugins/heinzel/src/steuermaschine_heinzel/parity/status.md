# status (read-only)

Compared on 2026-11-12 23:05 for the three public repositories, 6 open pull requests (1 ready, 5 needs work), with the
recorded real answers in `tests/fixtures/status/status-20261112.json`:

| Input | Old output | Result |
|---|---|---|
| no option | gate audit lines + slot line + `rows()` + `render(table, False)` | identical, line for line |
| `--md` | the same with `- ` gate bullets and markdown tables | identical |
| `--stale` | one line per pull request unchanged for over six hours | identical |

The table functions (`summary`, `failing`, `age`, `group_of`, `rows`, `stale`, `render`, `slot_line`, `normalise`) and
the gate reads (`sonar_state`, `coverage_state`, `required_in`, the audit line, `advisory_checks`) are moved verbatim.

Differs on purpose:

- The tool map row of the old status tool keeps its former path for now: a sentence of the alarm module is renamed
  through that row, and its parity test pins the result. `sm status` is listed under `also`.
- The repositories are settings (`status_repos`, `status_gate_repos`, `status_gate_notes`). The defaults are the three
  public repositories; a private repository and the one-line note for a repository without a gate are added in the
  settings file of the machine.
- A ruleset that GitHub did not answer for is marked `RULESET UNREAD` and listed under UNKNOWN; the old audit printed
  `NO RULESET` for it.
- A repository that could not be listed is an `UNKNOWN` line on stdout (and in the JSON document), not a line on stderr.
- `NO OWNER: ...` of `--check` is the last line on stdout; the exit code is still 1.
- The slot sizes are read live with the probe `go` uses (no cache is written). When GitHub does not answer, the last
  hourly read is used and the JSON document says `source: cache`.
- The reads of the repositories run side by side: about 2 seconds instead of about 20.
- Sonar is read through the core Sonar provider (`steuermaschine.github.sonar.Sonar.get`), the coverage badge with the
  same injected HTTP GET function (`providers.extra["http_fetch"]`, default the core `urllib_fetch`); tests inject a fake.
