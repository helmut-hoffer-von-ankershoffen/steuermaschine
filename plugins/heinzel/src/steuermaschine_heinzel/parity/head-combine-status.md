# Parity: `sm head combine status` and `sm head combine plan` against the former combine tool

Same code: selection, caps, plan and the printed lines are the moved read half of the combine module
(combine_lib/combine.py; tests/views_verbatim.py: verbatim except where the pinned files come from). The pure tests of
the old tool moved with it (areas and solo classes, member subject, plan selection rules: assertions kept). The command
tests run status and plan on a scripted `gh pr list` and check the lines the old status and plan printed.

Differs on purpose:
- Hints that named the old build command name `sm head combine build`; the `next:` line says the build is not ported.
- `--format json` carries the members, refusals, in-flight combines and the plan as fields.
- `--repo plugins` needs the setting `combine_plugins_repo` (no repository name is built in).

Not ported (writes): build, publish, ship, rebase-next, bisect. They push branches, open, close and merge PRs and
dispatch CI; they belong on top of `sm head ship` and `sm head pr`, which are not on main yet.
