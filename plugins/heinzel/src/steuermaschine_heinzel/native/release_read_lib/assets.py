"""hz-assets-health: probe the REAL GET (Range 0-100, never HEAD) of every external asset hooks and CI download.
200/206 = OK; 5xx, 4xx other than 416, timeouts = BAD. URLs are parsed from the core repo, not hardcoded."""
import argparse, json, os, re, sys, time, tomllib, urllib.request, urllib.error
from . import edge  # moved: records, processes and what used to be separate scripts
from ... import state as statedir  # the plugin's state paths

CORE = edge.core_checkout()  # moved: the read-only core checkout, from the plugin's settings
CACHE = str(statedir.path("assets-health.json"))
PLATFORMS = ("darwin-arm64", "linux-x86_64")
WHEEL_URL = ("plugin-tools wheel", "https://github.com/helmut-hoffer-von-ankershoffen/heinzel-plugins/releases/download/tools-v0.2.1/heinzel_plugin_tools-0.2.1-py3-none-any.whl")
UV_URL = ("uv", "https://github.com/astral-sh/uv/releases/latest/download/uv-aarch64-apple-darwin.tar.gz")


def tool_urls(path, platforms=PLATFORMS):
    """(name, url) for each tool in tools.toml and platform, with {version}/{file} filled in."""
    out = []
    for name, spec in tomllib.loads(open(path, encoding="utf-8").read()).items():
        for key, entry in spec.get("files", {}).items():
            if key in platforms:
                f = entry.get("file", "").format(version=spec["version"])
                u = entry.get("url", spec.get("url", "")).format(version=spec["version"], file=f)
                out.append((f"{name}:{key}", u))
    return out


def requirement_urls(path):
    """(name, url) for every https URL in a requirements file (wheel URLs); [] if the file does not exist."""
    if not os.path.exists(path):
        return []
    out = []
    for l in open(path, encoding="utf-8"):
        for u in re.findall(r"https://[^\s#;\"']+", l):
            out.append(("wheel:" + u.rsplit("/", 1)[-1].split("#")[0][:40], u))
    return out


def assets(core=CORE):
    a = requirement_urls(os.path.join(core, "scripts", "plugin-tooling-requirements.txt"))
    a += tool_urls(os.path.join(core, "scripts", "tools.toml"))
    if not any("heinzel_plugin_tools" in u for _, u in a):
        a.append(WHEEL_URL)
    return a + [UV_URL]


def probe(url, timeout=20, opener=urllib.request.urlopen):
    """(ok, status) from a real GET with Range bytes=0-100."""
    req = urllib.request.Request(url, method="GET", headers={"Range": "bytes=0-100", "User-Agent": "sm release assets"})
    try:
        with opener(req, timeout=timeout) as r:
            r.read(101)
            return r.status in (200, 206), r.status
    except urllib.error.HTTPError as e:
        return False, e.code
    except Exception as e:  # timeouts, DNS, TLS
        return False, type(e).__name__


def check(items, prober=probe):
    return [(n, u, *prober(u)) for n, u in items]


def digest_line(path=CACHE):
    try:
        d = json.load(open(path))
    except (OSError, ValueError):
        return "ASSETS: unknown (run sm release assets)"
    bad = [r["name"] for r in d["results"] if not r["ok"]]
    n = len(d["results"])
    age = int((time.time() - d["ts"]) / 60)
    tail = f" ({age} min old)" if age > 30 else ""
    return f"ASSETS: ok {n}/{n}{tail}" if not bad else f"ASSETS: BAD {len(bad)}/{n} ({', '.join(bad)[:90]}){tail}"


def save(results, path=CACHE):
    """Write the probe cache atomically: a temp file in the same directory, then os.replace, so a reader never sees half."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {"ts": time.time(), "results": [{"name": n, "url": u, "ok": ok, "status": s} for n, u, ok, s in results]}
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as handle:
            json.dump(data, handle)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def wait_stable(n, interval, rounds, sleep=time.sleep, max_rounds=240, *, report=False):
    """Run rounds() -> results until n consecutive all-OK rounds; returns (stable, results). Ceiling max_rounds."""
    streak, res = 0, []
    for round_number in range(1, max_rounds + 1):
        res = rounds()
        healthy = sum(result[2] for result in res)
        streak = streak + 1 if healthy == len(res) else 0
        if report:
            print(
                f"ASSETS: round {round_number}/{max_rounds}, {healthy}/{len(res)} OK, stable streak {streak}/{n}",
                file=sys.stderr,
                flush=True,
            )
        if streak >= n:
            return True, res
        sleep(interval)
    return False, res


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sm release assets", description=__doc__.split("\n")[0]
                                 + " Exit 0 all OK, 1 any BAD. Caches assets-health.json in the state directory (sm go --digest prints ASSETS:).")
    ap.add_argument("--stable", type=int, metavar="N", help="wait for N consecutive all-OK rounds, reporting progress to stderr; ceiling 240 rounds")
    ap.add_argument("--interval", type=int, default=30, metavar="S", help="seconds between rounds with --stable (30)")
    ap.add_argument("--core", default=CORE, help="core repo checkout (~/Code/heinzel)")
    ap.add_argument("--digest", action="store_true", help="print the cached digest line only")
    a = ap.parse_args(argv)
    if a.digest:
        print(digest_line()); return 0
    items = assets(a.core)
    stable, res = wait_stable(a.stable, a.interval, lambda: check(items), report=True) if a.stable else (None, check(items))
    save(res)
    for n, u, ok, s in res:
        print(f"{'ok ' if ok else 'BAD'} {str(s):<8} {n:<32} {u[:70]}")
    print(digest_line())
    return 0 if all(r[2] for r in res) and stable is not False else 1

