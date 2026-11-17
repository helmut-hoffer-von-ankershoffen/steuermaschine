# Reviewed full PR execution inputs

`sm head full-pr prepare` and `apply` authorize a narrowly reviewed Makefile change for ordinary complete Core PR CI. They do not record successful tests or select a reduced suite. Protected main must already expose the typed verifier and complete registry duplicate policy; a feature branch cannot supply its own verifier.

Use a clean named feature branch containing current protected main, before opening its PR. The only changed execution input may be Makefile. The protected verifier binds all changed paths, the predicted merge tree, every execution-input blob, repository, head and base. Source review additionally binds the repository ID, branch, verifier blob and producer files.

1. Run `sm head full-pr prepare WORKTREE --out PRIVATE_PACKET_DIR`.
2. Have an independent reviewer inspect `source.json`, its exact source and digest. Register the verdict with `sm review verdict SOURCE_DIGEST --approve --reviewer NAME --notes "review evidence"`.
3. Run the same prepare command with `--source-approved SOURCE_DIGEST`. Existing authenticated nonconsumer receipts may be supplied with `--nonconsumers FILE --review-root ROOT`.
4. Independently review `prepare.out` and register its digest verdict. This packet binds the complete before/after registry, exact append bytes and source review.
5. Run `sm head full-pr apply PRIVATE_PACKET_DIR --approved TRANSACTION_DIGEST`.

Apply repeats source, transport, capacity and consumer checks under existing queue/train/release/version exclusions and registry locking. It appends one slot, verifies complete readback, restores the queue and writes a private receipt. It never edits an existing entry or rewrites registry state after a mismatch. A durable canonical journal keyed by repository ID and head prevents replay after an ambiguous write, including a different output directory or transaction digest; inspect retained evidence before any new plan. Both reviews remain required by the supported command.

Continue with normal pinned push and PR creation, original full hosted CI, independent code review and normal merge admission. This source authorization is not CI proof, a release authorization or permission to retry failed CI. A changed head/base/source/producer or registry requires fresh preparation and review.
