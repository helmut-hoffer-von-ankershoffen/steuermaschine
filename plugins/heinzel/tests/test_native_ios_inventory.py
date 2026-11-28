import io
import json
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from steuermaschine_heinzel.native.release_lib import ios_inventory as asc
from steuermaschine_heinzel.native.release_lib.ios import internal_group_refusal

APP, GROUP, LOCALE = "8012345678", "Example Internal", "en-US"
BUILD = "9373ea3d-42b8-45fb-a35c-5edf76fcfac3"
OTHER = "f3492589-b3ca-43af-9057-b6e09d7cf903"
TAG = asc.TagIdentity(APP, GROUP, LOCALE)


def build_page(*, identifier=BUILD, number="36", version="0.9.57", expired=False, state="VALID"):
    return {
        "data": [
            {"id": identifier, "type": "builds", "attributes": {"version": number, "expired": expired, "processingState": state}, "relationships": {"preReleaseVersion": {"data": {"id": "version"}}}}
        ],
        "included": [{"id": "version", "type": "preReleaseVersions", "attributes": {"version": version}}],
        "meta": {"paging": {"total": 1}},
        "links": {"next": None},
    }


class Provider:
    def __init__(self, *, page=None, member=False, locale=False, group_flags=(True,)):
        self.page = page if page is not None else build_page()
        self.member = member
        self.locale = locale
        self.group_flags = group_flags
        self.requests = []

    def __call__(self, method, path, body=None):
        self.requests.append((method, path, body))
        if method != "GET":
            return {}
        if path.startswith("/v1/builds?"):
            assert f"filter%5Bapp%5D={APP}" in path
            return self.page
        if path.startswith(f"/v1/apps/{APP}/betaGroups?"):
            return {
                "data": [
                    {"type": "betaGroups", "id": "internal-group" if index == 0 else f"same-name-{index}", "attributes": {"name": GROUP, "isInternalGroup": internal}}
                    for index, internal in reversed(list(enumerate(self.group_flags)))
                ]
            }
        if path.startswith("/v1/betaGroups/internal-group/relationships/builds?"):
            return {"data": [{"type": "builds", "id": BUILD}] if self.member else []}
        if path == f"/v1/builds/{BUILD}/buildBetaDetail":
            return {"data": {"attributes": {"internalBuildState": "IN_BETA_TESTING"}}}
        if path.startswith(f"/v1/builds/{BUILD}/betaBuildLocalizations?"):
            return {"data": [{"type": "betaBuildLocalizations", "id": "existing-localization", "attributes": {"locale": LOCALE}}] if self.locale else []}
        pytest.fail(f"unexpected fake ASC request {method} {path}")

    def writes(self):
        return [request for request in self.requests if request[0] != "GET"]


@pytest.fixture
def tagged_tool(tmp_path):
    tool = tmp_path / "testflight"
    tool.write_text(f"APP_ID = {APP!r}\nGROUP_NAME = {GROUP!r}\nNOTES_LOCALE = {LOCALE!r}\nAPI = {asc.API!r}\nraise RuntimeError('never run the tagged tool')\n")
    return tool


def test_identity_uses_literal_fields_without_running_tagged_tool(tagged_tool):
    assert asc.tag_identity(tagged_tool) == TAG


@pytest.mark.parametrize("suffix", ["API = 'https://example.invalid'", "APP_ID = get_id()", "GROUP_NAME += ' suffix'"])
def test_identity_refuses_redefined_fields(tagged_tool, suffix):
    tagged_tool.write_text(tagged_tool.read_text() + suffix + "\n")
    with pytest.raises(ValueError):
        asc.tag_identity(tagged_tool)


def test_tag_origin_is_fixed_even_without_duplicate_assignment(tagged_tool):
    tagged_tool.write_text(tagged_tool.read_text().replace(asc.API, "https://example.invalid"))
    with pytest.raises(ValueError, match="API origin"):
        asc.tag_identity(tagged_tool)


@pytest.mark.parametrize("member", [True, False])
def test_assignment_checks_exact_build_last_and_writes_only_its_uuid(member):
    provider = Provider(member=member)
    report = asc.run(provider, TAG, "assign", "36", "0.9.57", BUILD)
    if member:
        assert report is None and not provider.writes()
    else:
        assert report == f"assigned build 36 ({BUILD})"
        assert provider.writes() == [("POST", "/v1/betaGroups/internal-group/relationships/builds", {"data": [{"type": "builds", "id": BUILD}]})]
        assert provider.requests[-2][1].startswith("/v1/builds?")


def test_beta_reads_exact_uuid_and_preserves_the_caller_text_contract():
    provider = Provider()
    assert asc.run(provider, TAG, "beta", "36", "0.9.57", BUILD) == "build 36 internalBuildState=IN_BETA_TESTING"
    assert provider.requests[-1] == ("GET", f"/v1/builds/{BUILD}/buildBetaDetail", None)


@pytest.mark.parametrize("locale", [True, False])
def test_notes_only_updates_the_exact_build_localization(locale):
    provider = Provider(locale=locale)
    assert asc.run(provider, TAG, "notes", "36", "0.9.57", BUILD, "# A [link](https://example.invalid) ✨") == "notes set for build 36 (6 characters)"
    if locale:
        assert provider.writes() == [
            (
                "PATCH",
                "/v1/betaBuildLocalizations/existing-localization",
                {
                    "data": {"type": "betaBuildLocalizations", "id": "existing-localization", "attributes": {"whatsNew": "A link"}},
                },
            )
        ]
    else:
        assert provider.writes() == [
            (
                "POST",
                "/v1/betaBuildLocalizations",
                {
                    "data": {"type": "betaBuildLocalizations", "attributes": {"whatsNew": "A link", "locale": LOCALE}, "relationships": {"build": {"data": {"type": "builds", "id": BUILD}}}},
                },
            )
        ]
    assert provider.requests[-2][1].startswith("/v1/builds?")


@pytest.mark.parametrize("change", [{"identifier": OTHER}, {"number": "37"}, {"state": "PROCESSING"}, {"expired": True}, {"version": "0.9.56"}])
def test_no_write_for_stale_build_identity(change):
    provider = Provider(page=build_page(**change))
    with pytest.raises(ValueError, match="does not match"):
        asc.run(provider, TAG, "assign", "36", "0.9.57", BUILD)
    assert not provider.writes()


def test_inventory_checks_complete_filtered_pages_and_ignores_no_redirect():
    first, second = build_page(), build_page(identifier=OTHER, number="37")
    first["meta"]["paging"]["total"] = second["meta"]["paging"]["total"] = 2
    first["links"]["next"] = asc.API + f"/v1/builds?filter%5Bapp%5D={APP}&include=preReleaseVersion&cursor=next"
    pages = iter([first, second])
    assert set(asc.inventory(lambda _: next(pages), APP)["builds"]) == {BUILD, OTHER}
    first["links"]["next"] = first["links"]["next"].replace(asc.API, "https://example.invalid")
    with pytest.raises(ValueError, match="pagination URL"):
        asc.inventory(lambda _: first, APP)


def test_runtime_credential_file_shell_shape_without_emitting_auth(tmp_path, monkeypatch, capsys):
    private = tmp_path / "fixture-private-key.p8"
    private.write_text("fictional private key")
    credentials = tmp_path / "fixture.env"
    credentials.write_text("# fixture\nexport ASC_KEY_ID='fixture-key-id'\nASC_ISSUER_ID=fictional-issuer\nASC_KEY_PATH=\"$EXPECTED_KEY\"\n")
    monkeypatch.setenv("ASC_ENV_FILE", str(credentials))
    monkeypatch.setenv("EXPECTED_KEY", str(private))

    def fake_encode(claims, key, *, algorithm, headers):
        assert claims["iss"] == "fictional-issuer" and claims["aud"] == "appstoreconnect-v1"
        assert claims["exp"] - claims["iat"] == 1200
        assert key == "fictional private key" and algorithm == "ES256" and headers == {"kid": "fixture-key-id"}
        return "fictional JWT"

    monkeypatch.setattr(asc.jwt, "encode", fake_encode)
    assert asc.auth_token() == "fictional JWT"
    assert capsys.readouterr() == ("", "")


def test_transport_refuses_off_origin_and_discards_unapproved_error_payload(monkeypatch):
    monkeypatch.setattr(asc, "auth_token", lambda: "fictional JWT")
    request = Request(asc.API + "/v1/builds")
    assert asc.NoRedirect().redirect_request(request, None, 307, "redirect", Message(), "https://example.invalid") is None

    class Opener:
        def open(self, request, *, timeout):
            assert request.full_url == asc.API + "/v1/builds"
            assert request.get_header("Authorization") == "Bearer fictional JWT" and timeout == 60
            raise HTTPError(request.full_url, 401, "private detail", Message(), io.BytesIO(b"private provider payload"))

    monkeypatch.setattr(asc, "build_opener", lambda *handlers: Opener())
    call = asc.client(120)
    with pytest.raises(ValueError, match="invalid local"):
        call("GET", "https://example.invalid/v1/builds")
    with pytest.raises(asc.AscRequestError, match=r"^ASC GET request failed: HTTP 401$"):
        call("GET", "/v1/builds")


@pytest.mark.parametrize("known", [True, False])
def test_only_exact_apple_422_preserves_previous_classification(known):
    path = "/v1/betaGroups/internal-group/relationships/builds"
    match = dict(asc.GROUP_REFUSAL, hidden="private provider material")
    if not known:
        match["detail"] = "different rejection"
    error = HTTPError(asc.API + path, 422, "unknown details", Message(), io.BytesIO(json.dumps({"errors": [match]}).encode()))
    message = str(asc.request_failure(error, "POST", path))
    assert internal_group_refusal(message) is known
    assert "private provider material" not in message and "different rejection" not in message


def test_in_process_inventory_reads_only_tag_identity(tagged_tool, monkeypatch):
    provider = Provider()
    monkeypatch.setattr(asc, "client", lambda timeout: provider)
    output = json.loads(asc.invoke(tagged_tool, "inventory", timeout=180))
    assert output["app"] == APP and output["builds"][BUILD]["short"] == "0.9.57"


def test_in_process_call_hides_tag_path_and_syntax_values(tmp_path):
    path = Path(tmp_path / "fictional-private-path")
    path.write_text("private invalid syntax !")
    with pytest.raises(asc.AscRequestError) as result:
        asc.invoke(path, "inventory")
    assert str(result.value) == "Could not verify App Store Connect identity or credentials."


def test_transport_caps_requests_to_remaining_monotonic_budget(monkeypatch):
    monkeypatch.setattr(asc, "auth_token", lambda: "fictional JWT")
    moments = iter([100.0, 100.0, 117.0, 125.0])
    monkeypatch.setattr(asc, "time", SimpleNamespace(monotonic=lambda: next(moments)))
    timeouts = []

    class Opener:
        def open(self, request, *, timeout):
            timeouts.append(timeout)
            return io.BytesIO(b"{}")

    monkeypatch.setattr(asc, "build_opener", lambda *handlers: Opener())
    call = asc.client(25)
    assert call("GET", "/v1/builds") == {}
    assert call("GET", "/v1/builds") == {}
    with pytest.raises(asc.AscRequestError, match="deadline exhausted"):
        call("GET", "/v1/builds")
    assert timeouts == [25, 8]


def test_status_keeps_duplicate_number_rows_and_marks_exact_internal_membership(tagged_tool, monkeypatch):
    page = build_page()
    unresolved = build_page(identifier=OTHER, state="PROCESSING")["data"][0]
    unresolved["relationships"]["preReleaseVersion"]["data"] = None
    page["data"] += [unresolved, build_page(identifier="ignored-id", number="37")["data"][0]]
    page["meta"]["paging"]["total"] = 3
    provider = Provider(page=page, member=True, group_flags=(True, False))
    monkeypatch.setattr(asc, "client", lambda timeout: provider)
    result = asc.invoke(tagged_tool, "status", "36")
    assert result.splitlines() == [
        f"0.9.57 (36) VALID expired=False in-group id={BUILD}",
        f"None (36) PROCESSING expired=False NOT-in-group id={OTHER}",
    ]
    assert not provider.writes()


@pytest.mark.parametrize("flags", [(False,), (True, True)])
def test_status_refuses_external_only_or_ambiguous_internal_group(flags):
    provider = Provider(group_flags=flags)
    with pytest.raises(ValueError, match="exactly one tagged internal"):
        asc.status(provider, TAG, "36")
    assert len(provider.requests) == 1 and not provider.writes()
