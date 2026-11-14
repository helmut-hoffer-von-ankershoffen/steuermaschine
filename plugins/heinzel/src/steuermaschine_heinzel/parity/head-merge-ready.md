# head merge-ready (read-only)

Compared live on 2026-11-12 23:40 for pull request 797 (`NO-GO #797: no review verdict on file`, same first reason)
and with the old tool's test cases (GO, every refusal, holds for Core and another repository, broken action table),
which moved with the check function.

Differs on purpose: the command only judges. The old `--apply` (squash-merge of the checked head and a line in the
interventions log) is a write and is not ported here; the JSON document carries `head`, the commit a merge must be
bound to. Without a configured action table (`go_pr_actions_file`) the answer is NO-GO: holds cannot be read.
