# ruff: noqa: SIM115, RUF100  (moved verbatim; see tests/ci_verbatim.py)
"""Thin stdlib client for the typed-question classifier "Jev" (TypeSafe AI System One, not an LLM). Advisory only.

Default OFF: nothing is sent unless CONFIG["mode"] is "on" AND a key exists. Mode "off" or the file <dir>/OFF is the
kill switch. The caller fills CONFIG from the settings (`configure`): mode, dir (state directory: the daily budget,
the kill switch, the one-hour pause after a rejected key), api_key, url (tests; https or loopback only),
keychain_item, run (the process runner, for the keychain read). The key is never logged, printed or stored. Every
failure returns None and callers keep their behaviour. Jev may sort and label; it never decides, closes, merges or
reruns. Only PUBLIC CI logs of the allowlisted repositories may be sent (check_repo)."""
import fcntl, json, os, random, re, sys, time, urllib.error, urllib.request  # noqa: E401, I001
from datetime import date

BASE = "https://api.typesafe.ai"
MODEL = "jev-1.14.0"
ALLOWED_REPOS = ("helmut-hoffer-von-ankershoffen/heinzel", "helmut-hoffer-von-ankershoffen/heinzel-plugins")
CLASSES = {"infra-flake": "network, runner or service flake unrelated to the code", "test-assertion": "a test assertion or test failure",
           "lint-or-types": "lint, format or type-check error", "build-or-deps": "build, install or dependency resolution error",
           "sonar-gate": "SonarCloud quality gate failure", "timeout": "a job or step timed out"}
MAX_STATE_CHARS = 96_000  # about 24k tokens at len/4
DAILY_TOKENS, DAILY_CALLS = 2_000_000, 2_000
KEY_RE = re.compile(r"apikey_([0-9a-f]{32})_\S+")
_sleep = time.sleep  # tests replace it
CONFIG: dict = {}  # mode, dir, api_key, url, keychain_item, run: set by the caller


def configure(**values):
    """Replace the configuration with the caller's settings."""
    CONFIG.clear()
    CONFIG.update(values)


def redact(s, key=None):
    """Remove the key (pattern and literal) from any text bound for a log, exception or output."""
    s = KEY_RE.sub(lambda m: f"apikey_{m.group(1)[:8]}...", str(s))
    return s.replace(key, "<redacted>") if key and len(key) > 8 else s


def state_dir():
    return os.path.expanduser(str(CONFIG.get("dir") or "~/.cache/jev"))


def log(event, **f):
    """Structured JSONL event on stderr; never state, never key."""
    print(redact(json.dumps({"event": event, **f}, default=str)), file=sys.stderr)


def key():
    k = str(CONFIG.get("api_key") or "").strip()
    if k:
        return k
    run, item = CONFIG.get("run"), CONFIG.get("keychain_item")
    if not run or not item:
        return ""
    r = run(["security", "find-generic-password", "-s", str(item), "-w"], timeout=5)
    return r.out.strip() if r.rc == 0 else ""


def killed():
    return str(CONFIG.get("mode") or "").lower() == "off" or os.path.exists(os.path.join(state_dir(), "OFF"))


def wanted():
    """Opt-in: default OFF, only mode on enables, kill switch wins."""
    return not killed() and str(CONFIG.get("mode") or "").lower() in ("on", "1", "true")


def available():
    """True when a call could be made (opt-in, not disabled by a 401, key present). Does not spend budget."""
    if not wanted() or _disabled():
        return False
    return bool(key())


def _disabled():
    try:
        return time.time() < float(open(os.path.join(state_dir(), "disabled-until")).read())
    except (OSError, ValueError):
        return False


def check_repo(repo):
    """Hard allowlist: only the public heinzel / heinzel-plugins repos. Anything else is refused."""
    return repo in ALLOWED_REPOS


def _base_url():
    u = CONFIG.get("url") or BASE
    if not (u.startswith("https://") or re.match(r"http://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", u)):
        return BASE
    return u.rstrip("/")


def _budget(tokens, calls=1, commit=True):
    """Check (and with commit, spend) the daily cap under a lock file. True if within budget."""
    os.makedirs(state_dir(), exist_ok=True)
    with open(os.path.join(state_dir(), "budget.lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        path = os.path.join(state_dir(), "budget.json")
        try:
            b = json.load(open(path))
        except (OSError, ValueError):
            b = {}
        today = date.today().isoformat()
        if b.get("day") != today:
            b = {"day": today, "input_tokens": 0, "calls": 0}
        if b["input_tokens"] + tokens > DAILY_TOKENS or b["calls"] + calls > DAILY_CALLS:
            return False
        if commit:
            b["input_tokens"] += tokens
            b["calls"] += calls
            json.dump(b, open(path, "w"))
        return True


def budget_use():
    try:
        return json.load(open(os.path.join(state_dir(), "budget.json")))
    except (OSError, ValueError):
        return {"day": date.today().isoformat(), "input_tokens": 0, "calls": 0}


def _http(method, path, k, body, timeout):
    req = urllib.request.Request(_base_url() + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {k}", "Content-Type": "application/json", "User-Agent": "ci-classifier/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}"), None
    except urllib.error.HTTPError as e:
        return e.code, None, e.headers.get("Retry-After")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return 0, None, None


def ask(state, questions, *, repo, model=MODEL, timeout=10.0, retries=2, tool=""):
    """POST a state and typed questions. -> response dict, or None on ANY problem (callers keep today's behaviour).
    `questions` is a list of {name, type: choice|noul|score, instructions, options|levels}. Never raises."""
    k = ""
    try:
        if not check_repo(repo):
            log("jev.skipped", tool=tool, reason="repo_not_allowed")
            return None
        if not wanted() or _disabled():
            return None
        if len(state) > MAX_STATE_CHARS:
            log("jev.skipped", tool=tool, reason="state_too_large", state_chars=len(state))
            return None
        k = key()
        if not k:
            return None
        est = len(state) // 4 + 1
        if not _budget(est):
            log("jev.skipped", tool=tool, reason="budget")
            return None
        log("jev.request", tool=tool, model=model, questions=[q.get("name") for q in questions], state_chars=len(state))
        t0, status = time.time(), 0
        for attempt in range(1, retries + 2):
            status, data, retry_after = _http("POST", "/v1/systemone", k, {"model": model, "state": state, "questions": questions}, timeout)
            if status == 200 and isinstance(data, dict):
                log("jev.outcome", tool=tool, status="ok", http=200, attempts=attempt, ms=int((time.time() - t0) * 1000),
                    input_tokens=(data.get("usage") or {}).get("input_tokens"))
                return data
            if status == 401:
                open(os.path.join(state_dir(), "disabled-until"), "w").write(str(time.time() + 3600))
                log("jev.disabled", tool=tool, hint="401: key rejected; paused for one hour")
                break
            if status in (400, 413, 415, 422) or (status == 200):
                break
            if attempt <= retries:
                try:
                    wait = min(float(retry_after), 10.0)
                except (TypeError, ValueError):
                    wait = (0.5, 2.0)[min(attempt - 1, 1)] + random.random() * 0.2
                _sleep(wait)
        log("jev.outcome", tool=tool, status="failed", http=status, attempts=attempt, ms=int((time.time() - t0) * 1000))
        return None
    except Exception as e:  # noqa: BLE001 - the contract is: never raise into a caller
        log("jev.outcome", tool=tool, status="error", error=redact(f"{type(e).__name__}: {e}", k))
        return None


def noul(name, instructions):
    return {"name": name, "type": "noul", "instructions": instructions}


def choice(name, instructions, options):
    return {"name": name, "type": "choice", "instructions": instructions, "options": options}


def score(name, instructions, levels):
    return {"name": name, "type": "score", "instructions": instructions, "levels": levels}


def answer(resp, name):
    """The answer dict for a question name from a response (dict or list shaped), else None."""
    a = (resp or {}).get("answers")
    if isinstance(a, dict):
        return a.get(name)
    for x in a or []:
        if isinstance(x, dict) and x.get("name") == name:
            return x
    return None


def confident(ans, threshold=0.8):
    """-> the choice label if its confidence >= threshold, True/False for a noul beyond 0.9/0.1, else None."""
    if not isinstance(ans, dict):
        return None
    if "choice" in ans:
        c = ans.get("confidence")
        return ans["choice"] if isinstance(c, (int, float)) and c >= threshold else None
    if isinstance(ans.get("noul"), (int, float)):
        return True if ans["noul"] >= 0.9 else False if ans["noul"] <= 0.1 else None
    return None


def classify_failure(text, repo, tool="faillog"):
    """Cause class of a condensed PUBLIC CI failure -> (class, probability) at confidence >= 0.8, else None.
    Advisory label only: it never decides anything."""
    resp = ask(text, [choice("cause", "What is the main cause of this CI failure?", dict(CLASSES))], tool=tool, repo=repo)
    ans = answer(resp, "cause")
    label = confident(ans)
    if label not in CLASSES:
        return None
    p = (ans.get("probabilities") or {}).get(label, ans.get("confidence"))
    return label, float(p)


def jev_line(text, repo):
    """`jev: <class> p=<prob>` for a condensed log, or None (off, no key, other repo, low confidence, any error)."""
    if repo is None or not check_repo(repo) or not wanted():
        return None
    r = classify_failure(text, repo)
    return f"jev: {r[0]} p={r[1]:.2f}" if r else None

