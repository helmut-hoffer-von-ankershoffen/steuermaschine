"""Traps: what went wrong at exactly this step before. From the root-cause analysis of the day no release shipped.
Keyed '<subject>.<verb>.<step>'; the engine prints the trap of the step a run stops at."""

TRAPS = {
    "head.admit.preflight": "Failures found after admission cost a full cycle (about 40 min): fix every numbered preflight failure in ONE new commit.",
    "head.admit.packet": "A packet binds this exact head AND this exact main. After any merge to main every prepared packet is stale: rebuild, never reuse.",
    "head.admit.prepare": "'truncated inventory' is retried with the identical command, never with a new packet. Start nothing on GitHub while prepare runs.",
    "head.admit.review": "The digest alone is not a review. A re-prepare changes the bytes and voids the verdict.",
    "head.admit.apply": "Apply is the one registry write. If it fails after a receipt exists, inspect first; never retry. Any active run without a receipt blocks it.",
    "head.admit.push": "Never validate a SHA that is or will be a PR head from another branch: required checks attach by SHA and the PR sits BLOCKED with every check green.",
    "head.admit.pr": "A changed head needs a new admission. Do not push a fix-up to this branch.",
    "head.admit.queue": "A queued green PR that is unmerged after 15 minutes is a stall, not a wait: `sm alarm check`.",
    "release.cut.rehearsal": "Nothing but a rehearsal on this main shows the candidate's failures before the cut. A cut without it costs about 85 minutes per failure.",
    "release.cut.pins": "A Core dependency change makes the plugin Consumer jobs fail until the plugins are relocked and the pin moved.",
    "release.cut.cut": "First-attempt rule: a failed candidate job is not rerun. Drop the candidate with the supported command, never by moving state files.",
    "release.cut.live": "Published is not live: only the verification of the running service closes the release.",
}
