"""openapi_loader and the naming helpers it relies on: the guard paths the rest of the
suite does not walk (odd specs, curated defaults that do not apply, the live/cache/snapshot
order). A wrong answer here changes every tool's contract at startup.

Run with:  ENABLE_AUTH=false pytest -q tests/test_openapi_loader.py
"""

from __future__ import annotations

import dataclasses
import json
import os

import httpx
import pytest

os.environ.setdefault("ENABLE_AUTH", "false")
os.environ.setdefault("JOBMOJITO_OPENAPI_URL", "http://127.0.0.1:1/unreachable")
os.environ.setdefault("FEATUREBASE_API_KEY", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_URL", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_ID", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_SECRET", "")

import naming  # noqa: E402
import openapi_loader as loader  # noqa: E402

ODD_PATHS = {"/odd": "not a path item", "/x": {"parameters": [{"name": "a"}], "post": "not an operation"}}


def _path_with(attribute: str) -> tuple[str, str, naming.ToolMeta]:
    for (method, path), meta in naming.TOOL_META.items():
        if getattr(meta, attribute):
            return method, path, meta
    raise AssertionError(f"no curated tool sets {attribute}")


# ---- every pass skips what is not an operation ----------------------------------------------


@pytest.mark.parametrize(
    "fn", [loader.inject_operation_ids, loader.apply_param_defaults, loader.inject_view_params]
)
def test_odd_path_items_are_skipped(fn):
    spec = {"paths": json.loads(json.dumps(ODD_PATHS))}
    assert fn(spec) == {"paths": ODD_PATHS}


# ---- inject_operation_ids -------------------------------------------------------------------


def test_an_operation_id_from_the_api_is_kept_and_an_unknown_route_stays_unnamed():
    method, path, _ = next((m, p, v) for (m, p), v in naming.TOOL_META.items())
    spec = {
        "paths": {
            path: {method.lower(): {"operationId": "from_the_api"}},
            "/brand-new": {"post": {}},
        }
    }
    loader.inject_operation_ids(spec)
    assert spec["paths"][path][method.lower()]["operationId"] == "from_the_api"
    assert "operationId" not in spec["paths"]["/brand-new"]["post"]


# ---- apply_param_defaults -------------------------------------------------------------------


def test_curated_defaults_cover_every_guard(monkeypatch):
    method, path, meta = _path_with("param_defaults")
    name, value = next(iter(dict(meta.param_defaults).items()))
    parameters = [
        {"name": "someone_else", "schema": {"type": "integer"}},
        {"name": name, "schema": "not a schema"},
        {"name": name, "schema": {"type": "integer", "default": value}},  # already right
        {"name": name, "schema": {"type": "integer", "minimum": value + 1}},  # below the range
        {"name": name, "schema": {"type": "integer", "maximum": value - 1}},  # above the range
        {"name": name, "schema": {"type": "integer", "default": value + 5}},  # applied
    ]
    spec = {"paths": {path: {method.lower(): {"parameters": parameters}}}}
    loader.apply_param_defaults(spec)
    assert parameters[1]["schema"] == "not a schema"
    assert parameters[2]["schema"]["default"] == value
    assert "default" not in parameters[3]["schema"]
    assert "default" not in parameters[4]["schema"]
    assert parameters[5]["schema"]["default"] == value


def test_a_route_with_no_curated_default_is_untouched():
    spec = {"paths": {"/brand-new": {"get": {"parameters": [{"name": "limit", "schema": {"default": 9}}]}}}}
    loader.apply_param_defaults(spec)
    assert spec["paths"]["/brand-new"]["get"]["parameters"][0]["schema"]["default"] == 9


# ---- _allow_null ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ({"type": "string"}, {"type": ["string", "null"], "nullable": True}),
        ({"type": "null"}, {"type": "null", "nullable": True}),
        ({"type": ["string", "number"]}, {"type": ["string", "number", "null"], "nullable": True}),
        ({"type": ["string", "null"]}, {"type": ["string", "null"], "nullable": True}),
        ({"enum": ["a"]}, {"enum": ["a", None]}),
        ({"enum": ["a", None]}, {"enum": ["a", None]}),
        ({}, {}),
    ],
)
def test_allow_null(before, after):
    loader._allow_null(before)
    assert before == after


# ---- live, then cache, then snapshot --------------------------------------------------------


class _Paths:
    """Stands in for config.settings, whose dataclass is frozen."""

    def __init__(self, tmp_path):
        self.openapi_url = "http://spec.test/openapi"
        self.cache_path = tmp_path / "cache" / "openapi.json"
        self.snapshot_path = tmp_path / "snapshot.json"


@pytest.fixture
def paths(tmp_path, monkeypatch):
    settings = _Paths(tmp_path)
    monkeypatch.setattr(loader, "settings", settings)
    return settings


def _answer(monkeypatch, response):
    def fake_get(url, **kwargs):
        if isinstance(response, Exception):
            raise response
        return httpx.Response(response[0], json=response[1], request=httpx.Request("GET", url))

    monkeypatch.setattr(loader.httpx, "get", fake_get)


def test_a_live_spec_is_prepared_and_cached(paths, monkeypatch):
    _answer(monkeypatch, (200, {"paths": {"/live": {"get": {}}}}))
    assert "/live" in loader.load_openapi_spec()["paths"]
    assert "/live" in json.loads(paths.cache_path.read_text())["paths"]


def test_a_cache_wins_over_the_snapshot_when_live_is_down(paths, monkeypatch):
    _answer(monkeypatch, httpx.ConnectError("down"))
    paths.snapshot_path.write_text(json.dumps({"paths": {"/snapshot": {}}}))
    paths.cache_path.parent.mkdir(parents=True)
    paths.cache_path.write_text(json.dumps({"paths": {"/cache": {}}}))
    assert "/cache" in loader.load_openapi_spec()["paths"]


def test_the_snapshot_is_the_last_source(paths, monkeypatch):
    _answer(monkeypatch, (500, {}))
    paths.snapshot_path.write_text(json.dumps({"paths": {"/snapshot": {}}}))
    assert "/snapshot" in loader.load_openapi_spec()["paths"]


def test_nothing_anywhere_is_an_error(paths, monkeypatch):
    _answer(monkeypatch, (200, {"not": "a spec"}))
    with pytest.raises(RuntimeError, match="Could not load"):
        loader.load_openapi_spec()


def test_a_live_answer_that_is_not_an_object_is_ignored(paths, monkeypatch):
    _answer(monkeypatch, (200, ["a", "list"]))
    assert loader._fetch_live() is None


# ---- naming helpers -------------------------------------------------------------------------


def test_view_helpers_without_views():
    _, _, meta = next((m, p, v) for (m, p), v in naming.TOOL_META.items() if not v.views)
    assert meta.effective_default_view == ""
    assert naming.view_parameter_schema(meta) is None


def test_an_unknown_default_view_falls_back_to_the_last_one():
    _, _, meta = _path_with("views")
    broken = dataclasses.replace(meta, default_view="no-such-view")
    assert broken.effective_default_view == meta.views[-1].name


def test_description_hint_for():
    (method, path), meta = next(iter(naming.TOOL_META.items()))
    assert naming.description_hint_for(method, path) == meta.hint
    assert naming.description_hint_for("GET", "/brand-new") is None
