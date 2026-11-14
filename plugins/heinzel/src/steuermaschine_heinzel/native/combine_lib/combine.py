"""Moved from the former combine tool (its module of the same job): the read half. Select draft member PRs per area,
the caps, the plan and the status lines. Building, publishing, shipping, rebasing and bisecting are not moved (they
push, open and close PRs: write commands of their own, on top of `sm head ship` and `sm head pr` once those exist).

Verbatim, except: the pinned files come from Core's reviewed release policy at Core's main (the tool parsed the release
policy file next to it), and the Combiner class carries only its read methods. tests/views_verbatim.py prints every
differing line. IO goes through the injected context (`gh`, `now`, `say`, `repo`, `plugins`) of sm head combine.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field



def approved_sources(gh, repo: str) -> tuple[str, ...] | None:
    """Pinned files: the `sources` of Core's reviewed release policy at the tip of Core's main (None: unreadable)."""
    from ..queue_lib import core_policy
    from ..queue_lib.release_candidate import CandidateError

    def get(path: str):
        code, out = gh("api", f"repos/{repo}/{path}")
        if code:
            raise CandidateError(f"{path} unreadable")
        return json.loads(out)

    try:
        return tuple(sorted(core_policy.at_commit_api(get, get("commits/main")["sha"]).sources))
    except (CandidateError, ValueError, KeyError, TypeError):
        return None


AREAS = ("web", "system", "plugin", "tools")


K_MIN, K_MAX = 4, 6


SOFT_CAP, HARD_CAP = 4, 8


WINDOW_MIN = 60


MAX_SUBJECT = 72


LIMITS = {"web": (3000, 60), "system": (1500, 60), "plugin": (3000, 60), "tools": (0, 0)}  # union lines, files


SCOPES = {"web": "web", "system": "system", "plugin": "plugin", "tools": "ci"}


PLUGIN_SCOPES = {"clock", "github", "atlassian", "google", "zoom"}


TOOLS_PREFIXES = (".github/", "scripts/")


TOOLS_FILES = ("Makefile", ".pre-commit-config.yaml", ".sonarcloud.properties")


NEUTRAL_PREFIXES = ("docs/", "research/", "design/")


NEUTRAL_SUFFIXES = (".md", ".mdx", ".rst", ".txt")


GENERATED = ("web/openapi.json", "web/src/api/schema.d.ts", "uv.lock", "web/package-lock.json", "CHANGELOG.md")


SOLO_TOKENS = {"update", "updates", "updater", "install", "installed", "installation", "uninstall", "auth", "recovery"}


PINNED: tuple[str, ...] | None = None  # read by Combiner from Core's release policy; None refuses every member


FAILING = {"FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}


TYPE = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[a-z-]+)\))?!?: (?P<summary>.+)$")


META = re.compile(r"<!-- hz-combine (?P<kv>.*?) -->")  # old name kept: the marker in PR bodies already on GitHub


def is_neutral(path: str) -> bool:
    return path.startswith(NEUTRAL_PREFIXES) or path.endswith(NEUTRAL_SUFFIXES)


def file_area(path: str, plugins: bool = False) -> str:
    """web | system | tools | plugin:<name> | plugin | neutral."""
    if is_neutral(path):
        return "neutral"
    if path.startswith(TOOLS_PREFIXES) or path in TOOLS_FILES:
        return "tools"
    if plugins:
        m = re.match(r"plugins/([^/]+)/", path)
        return f"plugin:{m.group(1)}" if m else "plugin"
    return "web" if path.startswith("web/") else "system"


def solo_file(path: str) -> bool:
    """update/install/auth/recovery code ships alone (its own focused review)."""
    if is_neutral(path):
        return False
    return bool(SOLO_TOKENS & set(re.split(r"[/_.\-]", path.lower())))


def member_area(paths: list[str], plugins: bool = False) -> str:
    areas = {file_area(p, plugins) for p in paths} - {"neutral"}
    if "tools" in areas:
        return "tools"
    if plugins:
        names = {a for a in areas if a.startswith("plugin:")}
        return names.pop() if len(names) == 1 and areas <= names | {"plugin"} else ("plugin" if areas else "neutral")
    if not areas:
        return "neutral"
    return "web" if areas == {"web"} else "system"


def area_of_label(labels: list[str]) -> str | None:
    for name in labels:
        if name.startswith("combine:") and name[8:] in AREAS:
            return name[8:]
    return None


def counted(path: str) -> bool:
    """Files that count for overlap: generated files are regenerated, docs fold anywhere (rule 3)."""
    return path not in GENERATED and not is_neutral(path)


@dataclass
class Member:
    number: int
    title: str
    head: str
    sha: str
    base: str = "main"
    draft: bool = True
    labels: list[str] = field(default_factory=list)
    mergeable: str = "MERGEABLE"
    files: list[str] = field(default_factory=list)
    lines: int = 0
    checks: list[str] = field(default_factory=list)
    created: str = ""
    body: str = ""
    area: str = ""
    reason: str = ""  # why not selectable, empty when ready

    @classmethod
    def from_gh(cls, d: dict, plugins: bool = False) -> "Member":
        files = [f["path"] for f in d.get("files") or []]
        checks = [(c.get("conclusion") or c.get("state") or "").upper() for c in d.get("statusCheckRollup") or []]
        m = cls(number=d["number"], title=d.get("title", ""), head=d.get("headRefName", ""), sha=d.get("headRefOid", ""),
                base=d.get("baseRefName", "main"), draft=bool(d.get("isDraft")),
                labels=[x["name"] if isinstance(x, dict) else x for x in d.get("labels") or []],
                mergeable=d.get("mergeable") or "UNKNOWN", files=files,
                lines=sum((f.get("additions") or 0) + (f.get("deletions") or 0) for f in d.get("files") or []),
                checks=checks, created=d.get("createdAt", ""), body=d.get("body") or "")
        inferred = member_area(files, plugins)
        m.area = area_of_label(m.labels) or inferred
        if plugins and m.area == "plugin" and inferred.startswith("plugin:"):
            m.area = inferred  # one combine per plugin
        return m

    @property
    def area_root(self) -> str:
        return self.area.split(":")[0]


def refusal(m: Member, area: str, explicit: bool = False) -> str:
    """Why `m` cannot join a combine of `area` (empty = it can)."""
    if m.head.startswith(("release/", "combine/")):
        return "release or combine branch"
    if not m.draft:
        return "not a draft (it pushes its own CI: ship it alone)"
    if not explicit and not area_of_label(m.labels):
        return f"no combine:<area> label (gh pr edit {m.number} --add-label combine:{m.area_root if m.area_root in AREAS else area})"
    if m.mergeable == "CONFLICTING":
        return "conflicts with main: merge origin/main into it"
    if bad := sorted(set(m.checks) & FAILING):
        return f"draft checks failing ({', '.join(bad)})"
    if PINNED is None:
        return "pinned files unknown: Core's release policy (scripts/release_policy.json) at main is unreadable"
    pinned = [p for p in m.files if p in PINNED]
    if pinned and area != "tools":
        return f"touches pinned {pinned[0]} (Core's release policy): area tools, ships alone, re-pin after merge"
    if solo := [p for p in m.files if solo_file(p)]:
        return f"solo class (update/install/auth/recovery: {solo[0]}): ships alone with its own review"
    if area == "tools":
        return "tools/ci changes are never combined (k = 1): ship it alone"
    return ""


def topo_order(members: list[Member]) -> list[Member]:
    """Stacked bases first (a member whose base is another member's head comes after it), then by number."""
    by_head = {m.head: m for m in members}
    out, seen = [], set()

    def visit(m: Member, stack=()):
        if m.number in seen or m.number in stack:
            return
        if (b := by_head.get(m.base)) and b is not m:
            visit(b, stack + (m.number,))
        seen.add(m.number)
        out.append(m)

    for m in sorted(members, key=lambda x: x.number):
        visit(m)
    return out


def clusters(members: list[Member]) -> list[list[Member]]:
    """Members that share a counted file or a stack go into the same combine (chaining rule)."""
    parent = {m.number: m.number for m in members}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_file: dict[str, int] = {}
    by_head = {m.head: m.number for m in members}
    for m in members:
        for p in filter(counted, m.files):
            if p in by_file:
                parent[find(m.number)] = find(by_file[p])
            by_file.setdefault(p, m.number)
        if m.base in by_head:
            parent[find(m.number)] = find(by_head[m.base])
    groups: dict[int, list[Member]] = {}
    for m in members:
        groups.setdefault(find(m.number), []).append(m)
    return sorted(groups.values(), key=lambda g: min((x.created or "~", x.number) for x in g))


@dataclass
class Plan:
    area: str
    members: list[Member]
    deferred: list[tuple[Member, str]]
    refused: list[tuple[Member, str]]
    files: list[str]
    lines: int
    after: int | None = None  # in-flight combine this one is chained after
    overlap: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def plan(members: list[Member], area: str, inflight: list[dict] | None = None, k_max: int = K_MAX,
         explicit: tuple[int, ...] = (), without: tuple[int, ...] = ()) -> Plan:
    """Select up to k_max ready members of `area`, whole overlap clusters first-come, stacked bases first."""
    want = area if ":" in area else None
    root = area.split(":")[0]
    pool, refused = [], []
    taken = {n: c["number"] for c in inflight or [] for n, _ in c.get("members", [])}
    for m in members:
        if m.number in taken:
            if explicit and m.number in explicit:
                refused.append((m, f"already in combine #{taken[m.number]}"))
            continue
        if m.number in without:
            continue
        if explicit and m.number not in explicit:
            continue
        if not explicit and m.area_root != root and m.area != "neutral":
            continue
        if not explicit and want and m.area != want:
            continue
        if why := refusal(m, root, explicit=bool(explicit)):
            refused.append((m, why))
        else:
            pool.append(m)
    heads = {m.head for m in pool}
    stacked_refused = {m.head for m, _ in refused}
    ok = []
    for m in pool:
        if m.base != "main" and m.base not in heads:
            why = f"stacked on {m.base}, which is not a ready member" + (" (refused above)" if m.base in stacked_refused else "")
            refused.append((m, why))
        else:
            ok.append(m)
    max_lines, max_files = LIMITS.get(root, (3000, 60))
    chosen, deferred, files, lines = [], [], set(), 0
    for group in clusters(ok):
        g_files = {p for m in group for p in m.files}
        g_lines = sum(m.lines for m in group)
        fits = len(chosen) + len(group) <= k_max and lines + g_lines <= max_lines and len(files | g_files) <= max_files
        if fits or not chosen:
            take = topo_order(group)
            if not fits:  # a single cluster bigger than the window: oldest part now, the rest next window
                take, rest = take[:k_max], take[k_max:]
                deferred += [(m, "overlap cluster larger than one combine: next window") for m in rest]
            chosen += take
            files |= {p for m in take for p in m.files}
            lines += sum(m.lines for m in take)
        else:
            deferred += [(m, "window full (k, lines or files): next combine") for m in group]
    chosen = topo_order(chosen)
    p = Plan(area, chosen, deferred, refused, sorted(files), lines)
    mine = {f for f in files if counted(f)}
    best = None
    for c in inflight or []:
        shared = sorted(mine & {f for f in c.get("files", []) if counted(f)})
        if shared and (best is None or len(shared) > len(best[1])):
            best = (c["number"], shared)
    if best:
        p.after, p.overlap = best
        p.notes.append(f"overlaps in-flight combine #{best[0]} on {len(best[1])} file(s): built on top of it, ships after it (rule 1-2)")
        if len(best[1]) > 3:
            p.notes.append(f"overlap > 3 files with #{best[0]}: fold both into one PR before CI starts (rule 5)")
    if 0 < len(chosen) < K_MIN:
        p.notes.append(f"{len(chosen)} member(s) < {K_MIN}: build only when the window ({WINDOW_MIN} min) is over")
    return p


def strip_suffix(title: str) -> str:
    return re.sub(r"\s*\(#\d+\)\s*$", "", title.strip())


def shorten(text: str, room: int) -> str:
    if len(text) <= room:
        return text
    cut = text[: room + 1].rsplit(" ", 1)[0] if " " in text[: room + 1] else text[:room]
    return cut[:room].rstrip(" ,;:.-")


def member_subject(title: str, number: int, limit: int = MAX_SUBJECT) -> str:
    """`<member title> (#N)` within `limit` chars incl. the suffix; the summary is shortened, never the type(scope)."""
    t, suffix = strip_suffix(title), f" (#{number})"
    room = limit - len(suffix)
    if len(t) <= room:
        return t + suffix
    m = TYPE.match(t)
    head = t[: m.start("summary")] if m else ""
    return head + shorten(t[len(head):], room - len(head)) + suffix


def parse_meta(body: str) -> dict:
    m = META.search(body or "")
    if not m:
        return {}
    kv = dict(x.split("=", 1) for x in m.group("kv").split() if "=" in x)
    kv["members"] = [(int(n), sha) for n, sha in (x.split(":", 1) for x in kv.get("members", "").split(",") if ":" in x)]
    kv["after"] = int(kv["after"]) if kv.get("after", "-").isdigit() else None
    return kv


def cap_line(inflight: int) -> str:
    state = "HARD CAP" if inflight >= HARD_CAP else ("FULL" if inflight >= SOFT_CAP else "ok")
    return f"combines in flight {inflight}/{SOFT_CAP} (hard cap {HARD_CAP}): {state}"


def age_min(created: str, now: float) -> int:
    try:
        t = time.mktime(time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except ValueError:
        return 0
    return max(0, int((now - t) // 60))


PR_FIELDS = "number,title,headRefName,headRefOid,baseRefName,isDraft,labels,mergeable,files,statusCheckRollup,createdAt,body"


class Combiner:

    """The read half of the combiner (status and plan); building, shipping and bisecting are not ported."""



    def __init__(self, ctx: Ctx):
        global PINNED
        self.c = ctx
        PINNED = approved_sources(ctx.gh, ctx.repo)

    def open_prs(self) -> list[dict]:
        rc, out = self.c.gh("pr", "list", "-R", self.c.repo, "--state", "open", "--limit", "100", "--json", PR_FIELDS)
        if rc:
            raise SystemExit(f"gh pr list failed: {out[:200]}")
        return json.loads(out or "[]")

    def split(self, prs):
        combines = [p for p in prs if p.get("headRefName", "").startswith("combine/")]
        members = [Member.from_gh(p, self.c.plugins) for p in prs if p not in combines]
        inflight = [{"number": p["number"], "files": [f["path"] for f in p.get("files") or []],
                     **parse_meta(p.get("body", ""))} for p in combines]
        return members, inflight

    def area_for(self, area: str, plugin: str | None, members: list[Member]) -> str:
        if area != "plugin":
            return area
        if plugin:
            return f"plugin:{plugin}"
        ready = Counter(m.area for m in members if m.area.startswith("plugin:") and not refusal(m, "plugin"))
        if not ready:
            raise SystemExit("no ready plugin members (one combine per plugin: --plugin NAME)")
        return ready.most_common(1)[0][0]

    def plan(self, area, plugin=None, explicit=(), without=()):
        members, inflight = self.split(self.open_prs())
        full = self.area_for(area, plugin, members)
        return plan(members, full, inflight, explicit=tuple(explicit), without=tuple(without)), inflight

    def print_plan(self, p: Plan, inflight):
        c = self.c
        c.say(f"area {p.area}: {len(p.members)} member(s), {p.lines} lines, {len(p.files)} files; {cap_line(len(inflight))}")
        for m in p.members:
            c.say(f"  + #{m.number} {member_subject(m.title, m.number)}  [{m.head} {m.sha[:8]}{', on ' + m.base if m.base != 'main' else ''}]")
        for m, why in p.deferred:
            c.say(f"  ~ #{m.number} {strip_suffix(m.title)[:60]}: {why}")
        solo = [m.number for m, why in p.refused if why.startswith("not a draft")]
        for m, why in p.refused:
            if not why.startswith("not a draft"):
                c.say(f"  - #{m.number} {strip_suffix(m.title)[:60]}: {why}")
        if solo:
            c.say(f"  - not drafts (own CI, ship alone): " + " ".join(f"#{n}" for n in solo))
        for n in p.notes:
            c.say(f"  note: {n}")
        if p.members:
            c.say(f"next: combine build --area {p.area.split(':')[0]}" + (f" --plugin {p.area.split(':')[1]}" if ":" in p.area else ""))

    def status(self):
        members, inflight = self.split(self.open_prs())
        now = self.c.now()
        self.c.say(cap_line(len(inflight)))
        for f in inflight:
            n_m = len(f.get("members", []))
            self.c.say(f"  #{f['number']} {f.get('branch', '?')} area {f.get('area', '?')}, {n_m} member(s)"
                       + (f", after #{f['after']}" if f.get("after") else ""))
        by_area: dict[str, list[Member]] = {}
        for m in members:
            if m.draft and not m.head.startswith("release/"):
                by_area.setdefault(m.area, []).append(m)
        for area in sorted(by_area):
            ms = by_area[area]
            ready = [m for m in ms if not refusal(m, area.split(":")[0])]
            oldest = max((age_min(m.created, now) for m in ready), default=0)
            due = len(ready) >= K_MIN or (ready and oldest >= WINDOW_MIN)
            self.c.say(f"  {area}: {len(ready)} ready of {len(ms)} draft, oldest {oldest} min"
                       + (f" -> combine build --area {area.split(':')[0]}" if due and area.split(':')[0] in ("web", "system", "plugin") else ""))
        return 0
