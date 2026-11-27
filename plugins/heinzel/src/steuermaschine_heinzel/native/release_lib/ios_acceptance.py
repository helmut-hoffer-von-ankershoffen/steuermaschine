"""Read-only official release evidence required before replacing an existing TestFlight build."""

import hashlib
import json
from pathlib import Path
from typing import Any

from steuermaschine.config import child_environment

from ..queue_lib import hzlib
from . import snapshot


def accepted_marker(marker: Any, version: str) -> bool:
    """The public acceptance marker consumed by the release follower."""
    return isinstance(marker, dict) and marker.get("project") == "heinzel" and marker.get("version") == version and marker.get("accepted") is True


def require_replacement_authority(w: Any, version: str, revision: str, cwd: Path) -> None:
    """Bind the official final release, verdict bytes, published producer source and exact Git tag before ASC."""
    tag = f"v{version}"
    repo = hzlib.CORE
    if w.repo != repo:
        raise ValueError("iOS replacement requires the canonical Core repository")

    def api(path: str, repository: str = repo) -> dict[str, Any]:
        document, why = w.gh.try_json(["api", "--hostname", "github.com", f"repos/{repository}/{path}"])
        if why or not isinstance(document, dict):
            raise ValueError(f"cannot authenticate {repository}/{path}")
        return document

    release = api(f"releases/tags/{tag}")
    if release.get("tag_name") != tag or not isinstance(release.get("assets"), list) or not all(isinstance(a, dict) for a in release["assets"]):
        raise ValueError("canonical published release differs from the requested tag")

    def asset(name: str) -> dict[str, Any]:
        digest = snapshot.asset_digest(release, name)  # unique SHA-256 from the published, final release
        info = next(a for a in release["assets"] if a["name"] == name)
        asset_id = info.get("id")
        if type(asset_id) is not int or asset_id < 1:
            raise ValueError(f"{name} has no official asset ID")
        rc, raw = w.commands.run_bytes(["gh", "api", "--hostname", "github.com", f"repos/{repo}/releases/assets/{asset_id}",
                                        "-H", "Accept: application/octet-stream"], cwd=str(cwd), timeout=60, env=child_environment())
        if rc or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError(f"{name} differs from the official release asset")
        doc = json.loads(raw)
        if not isinstance(doc, dict) or type(doc.get("schema")) is not int or doc["schema"] != 1:
            raise ValueError(f"{name} has an unrecognized schema")
        return doc

    marker = asset("acceptance.json")
    if not accepted_marker(marker, version) or type(marker.get("acceptance_run")) is not int or marker["acceptance_run"] < 1:
        raise ValueError("Core release lacks an accepted verdict for this version")
    manifest = asset("artifact-manifest.json")
    if manifest.get("project") != "heinzel" or manifest.get("version") != version or manifest.get("commit") != revision:
        raise ValueError("published release artifact source differs from the requested tag revision")
    # Hosted verdicts additionally pin wheel and composed profile. The local release driver's schema-1 verdict omits both.
    if "wheel_sha256" in marker or "profile_sha256" in marker:
        wheel = snapshot.asset_digest(release, f"heinzel-{version}-py3-none-any.whl")
        app = api(f"releases/tags/app-{tag}", hzlib.PLUGINS)
        if (app.get("tag_name") != f"app-{tag}" or not isinstance(app.get("assets"), list) or not all(isinstance(a, dict) for a in app["assets"])
                or marker.get("wheel_sha256") != wheel or marker.get("profile_sha256") != snapshot.asset_digest(app, "standard.json")):
            raise ValueError("acceptance marker differs from the published wheel or profile")
    ref = api(f"git/ref/tags/{tag}")
    if ref.get("ref") != f"refs/tags/{tag}":
        raise ValueError("canonical release tag ref differs")
    obj = ref.get("object")
    for _ in range(4):
        if not isinstance(obj, dict) or not isinstance(obj.get("sha"), str) or not snapshot.COMMIT.fullmatch(obj["sha"]):
            break
        if obj.get("type") == "commit" and obj["sha"] == revision:
            return
        if obj.get("type") != "tag":
            break
        annotated = api(f"git/tags/{obj['sha']}")
        if annotated.get("sha") != obj["sha"]:
            break
        obj = annotated.get("object")
    raise ValueError("canonical release tag does not resolve to the requested commit")
