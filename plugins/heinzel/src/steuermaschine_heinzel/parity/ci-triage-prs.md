# ci triage prs | review | deadline (read-only)

Compared live on 2026-11-12 23:40 with the old read-only triage tool for the five open non-draft Core pull requests:

| Input | Result |
|---|---|
| `prs` | the same five lines; the commands after the arrow are sm commands (`sm head ship N`, `sm ci why N`, `sm ci rerun`, `sm ci one rerun N --job NAME --wait`, `sm head push`) |
| `deadline --due 784 --due 797` | identical, four lines |
| `review 784` | identical |

The 18 definitions of the rules (`classify_pr`, `triage_pr`, `review_line`, the deadline classification) are moved
word for word (`tests/ci_verbatim.py`); the old tool's 35 test cases moved with them and run against a scripted `gh`.

Differs on purpose: no optional class suffix on FAIL lines (the outside classifier is `sm ci classify`); a failure
of the read is a refusal with exit 1 on stderr (as before for `deadline`, new for `prs`, which printed nothing).
The registry and the review files are found through the settings `ci_known_failures_file` and `ci_research_dir`
(no defaults: without the first nothing is KNOWN and `deadline` refuses; without the second `review` refuses).
