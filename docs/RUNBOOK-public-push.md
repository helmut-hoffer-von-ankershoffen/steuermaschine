# Public push metadata

Run normal `sm head push PR|BRANCH` from the repository before pushing. Its commit-meta admission, also used by
`sm head pr`, checks the local proposed branch against `refs/remotes/origin/main`. A PR number selects that PR's
branch locally; its already-published commit list is not evidence for new local commits.

The check reads literal Git objects, including merge-side commits and full message bodies and trailers. It applies
the shared confidentiality rules to committed author and committer metadata and rejects machine-hostname emails.
Changing the current Git identity does not repair existing commits. Findings identify only commit position, field,
line and rule number, never matched text. Public-main ancestry is excluded; no CI-only author-email exemption is used.

Missing terms, missing refs, shallow history and range changes refuse admission. Configure `preflight_terms_file`
outside the repository. This check supplements the existing subject/title checks, public-root hook and hosted gates.
The explicit `--gate-only` scheduling query remains only a scheduling query, not metadata admission.
