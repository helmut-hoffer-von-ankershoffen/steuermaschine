"""Full local gates that `sm gate run` refuses (exit 2) unless --allow-full is given: the Mini must not run full coverage/preflight.

Refused: make pr (always: also with NO_PREFLIGHT=1 or --allow-full; the route is `sm head pr`) / preflight / check / coverage, pytest without a path argument,
vitest run without a file filter. Allowed: pytest tests/..., vitest run src/..., make check-fast, git push, anything else."""
import os
import shlex

ROUTE = ("Allowed route: `sm head pr BEAD TITLE` (checks and prints what it would run), then the same with `--live`; focused tests (`sm gate run -- pytest tests/path_test.py`, "
         "`sm gate run -- pnpm ... vitest run src/file.test.ts`, `sm gate run -- make check-fast`); hosted CI is the full gate. "
         "Override only with --allow-full (coordinator's call; never for make pr).")
NEVER_MAKE_PR = "`make pr` is never run on this machine (with or without NO_PREFLIGHT=1)"
FULL_MAKE = {"pr", "preflight", "check", "coverage"}
PATHLESS_OPTS_WITH_VALUE = {"-n", "-k", "-m", "-p", "-c", "--maxfail", "--timeout", "-o", "--cov", "--cov-report", "--durations",
                            "--workers", "--maxWorkers", "--reporter", "--config", "-C", "--dir", "--filter", "-F", "--session-timeout"}


def _unwrap(argv):
    """Drop leading `env [A=b ...]` and A=b words; return (env assignments, rest)."""
    env, words = {}, list(argv)
    if words[:1] == ["env"]:
        words = words[1:]
    while words and "=" in words[0] and not words[0].startswith("-") and words[0].split("=", 1)[0].isidentifier():
        k, v = words.pop(0).split("=", 1)
        env[k] = v
    return env, words


def _positionals(args):
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a.startswith("-"):
            skip = "=" not in a and a in PATHLESS_OPTS_WITH_VALUE
        else:
            out.append(a)
    return out


WHOLE_TREE = {".", "./", "tests", "tests/", "./tests", "./tests/", "src", "src/", "./src", "./src/", "web", "web/"}
UV_VALUE_OPTS = {"--with", "--python", "-p", "--project", "--directory", "--extra", "--group", "--with-requirements", "--env-file"}


def _strip_uv(words):
    """`uv run [opts] CMD...` -> CMD...; a --script run is not a gate."""
    rest, out, i = words[2:], [], 0
    while i < len(rest):
        w = rest[i]
        if w == "--script":
            return None
        if w == "--":
            return rest[i + 1:]
        if w.startswith("-"):
            i += 2 if (w in UV_VALUE_OPTS) else 1
            continue
        return rest[i:]
    return out


def _check(words, env, environ):
    if not words:
        return None
    words = list(words)
    if words[:1] == ["env"] or ("=" in words[0] and words[0].split("=", 1)[0].isidentifier()):
        e2, words = _unwrap(words)
        env = {**env, **e2}
        return _check(words, env, environ)
    head = os.path.basename(words[0])
    if (head in ("bash", "sh", "zsh") and "-c" in words[1:]) or (head in ("bash", "sh", "zsh") and any(w.startswith("-") and w.endswith("c") and not w.startswith("--") for w in words[1:])):
        idx = max(i for i, w in enumerate(words) if w.startswith("-") and not w.startswith("--") and w.endswith("c"))
        if idx + 1 < len(words):
            return _shell_string(words[idx + 1], env, environ)
        return None
    if head == "uv" and words[1:2] == ["run"]:
        inner = _strip_uv(words)
        return _check(inner, env, environ) if inner else None
    if head in ("nice", "time", "caffeinate") and len(words) > 1:
        return _check([w for w in words[1:] if not w.startswith("-")], env, environ)
    if head == "make":
        targets = [w for w in words[1:] if not w.startswith("-") and "=" not in w]
        hit = [t for t in targets if t in FULL_MAKE or t == "test" or t.startswith("test-")]
        if "pr" in hit:
            return NEVER_MAKE_PR
        return f"`make {hit[0]}` runs the full preflight/coverage/test suite" if hit else None
    pytest_rest = None
    if head in ("pytest", "py.test"):
        pytest_rest = words[1:]
    elif head.startswith("python") and words[1:3] == ["-m", "pytest"]:
        pytest_rest = words[3:]
    if pytest_rest is not None:
        pos = _positionals(pytest_rest)
        if not any(("/" in p or p.endswith(".py") or "::" in p or p in ("tests", "src")) and p not in WHOLE_TREE for p in pos):
            return "`pytest` without a specific test path runs the whole suite"
    if head in ("pnpm", "npm", "npx", "yarn", "bun") or "vitest" in words:
        pos = _positionals(words[1:])
        if "vitest" in words:
            rest = [p for p in _positionals(words[words.index("vitest") + 1:]) if p != "run"]
            if not rest or all(p in WHOLE_TREE for p in rest):
                return "`vitest` without a file filter runs every test"
        elif (pos and pos[0] in ("test", "t")) or pos[:2] == ["run", "test"] or pos[:2] == ["exec", "test"]:
            extra = pos[1:] if pos[0] in ("test", "t") else pos[2:]
            if not extra:
                return f"`{head} test` runs the whole JS suite"
    return None


def _shell_string(text, env, environ):
    import re
    for part in re.split(r"&&|\|\||;|\|", text):
        try:
            words = shlex.split(part)
        except ValueError:
            continue
        why = _check(words, env, environ)
        if why:
            return why
    return None


def refusal(argv, environ):
    """The reason text when the command is a full local gate, else None. `environ` is the caller's environment
    (kept for the callers; no variable allows `make pr` any more)."""
    env, words = _unwrap(argv)
    return _check(words, env, environ)
