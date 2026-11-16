# Full PR capacity recovery proof

The queue can ask protected Core for a separate `full-pr-capacity/v1` proof when the latest full PR run completed successfully on attempt 2. This is not an ordinary original-attempt proof, a focused plan, a release candidate, or a release acceptance receipt. Attempt 3 and later are refused.

The reader is still loaded from the independently observed current main commit, never from the PR or the working tree. Older protected Core without `verify_full_pr_capacity` refuses. Deploying this adapter cannot bootstrap a PR that introduces its own verifier.

Core owns the reviewed job inventory, authenticated GitHub capacity annotations, source identities and coverage verification. Its capacity verifier must reject executed workload failures, assigned runners or executed steps in a cancelled cell, missing or ambiguous jobs, unverifiable annotations, unrelated runs, changed sources and missing original coverage. Only original jobs that never acquired a hosted runner may be replaced, with the dependent aggregate failure accounted for. The recovered run must supply the complete successful workload under the same run, head, base and resulting tree.

The adapter calls that distinct protected API and requires exactly:

```json
{"type":"full-pr-capacity/v1","head":"<head>","base":"<main>","tree":"<resulting-tree>","run":123,"attempt":2,"original_attempt":1}
```

The numeric fields must be integers, not booleans. Extra fields, another proof type, changed identities, unavailable support or a refusal cannot become approval. The adapter rechecks the current PR merge tree and main after verification. The original-attempt route, candidate verification, release rules and published acceptance remain unchanged. Retain the original failure evidence; recovered success is not labelled a first-attempt pass.
