"""The ATS tools (2.0.0) and the merged tool set.

Run with:  ENABLE_AUTH=false pytest -q tests/test_ats_tools.py

Twenty endpoints became tools, and eleven older tools were folded into others
through an optional parameter (their endpoints stay in the API, hidden here).
These pin the listing (titles, read or write), the hidden endpoints, the
``is_ignored`` rule the route maps use, and that every parameter a hint names is
one the model can actually send.
"""

from __future__ import annotations

import json
import os
import pathlib
import re

import pytest

os.environ.setdefault("ENABLE_AUTH", "false")
os.environ.setdefault("JOBMOJITO_OPENAPI_URL", "http://127.0.0.1:1/unreachable")
os.environ.setdefault("FEATUREBASE_API_KEY", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_URL", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_ID", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_SECRET", "")

from naming import IGNORED_OPERATIONS, IGNORED_PATHS, TOOL_META, is_ignored  # noqa: E402

SNAPSHOT = json.loads((pathlib.Path(__file__).resolve().parent.parent / "data/openapi.snapshot.json").read_text())

# name -> (method, path, title, read_only)
NEW_TOOLS = {
    "set_position_process": ("POST", "/merchant-position-process-save", "Set a position's steps", False),
    "update_position_step": ("POST", "/merchant-position-step-update", "Change a position step's settings", False),
    "manage_meeting_slot": ("POST", "/merchant-meeting-slot-manage", "Create, change or delete a meeting slot", False),
    "duplicate_position": ("POST", "/merchant-position-duplicate", "Copy a position", False),
    "duplicate_interview": ("POST", "/merchant-interview-duplicate", "Copy an interview", False),
    "get_candidate": ("GET", "/merchant-candidate-get", "Get a candidate", True),
    "add_candidate_note": ("POST", "/merchant-candidate-note-add", "Add a note to a candidate", False),
    "set_candidate_tags": ("POST", "/merchant-candidate-tags-set", "Tag candidates", False),
    "manage_candidate_tag": ("POST", "/merchant-candidate-tag-manage", "Create, change or delete a candidate tag", False),
    "list_candidate_filters": ("GET", "/merchant-candidate-filter-options", "List candidate search filters", True),
    "preview_candidate_merge": ("GET", "/merchant-candidate-merge-preview", "Preview merging two candidates", True),
    "merge_candidates": ("POST", "/merchant-candidate-merge", "Merge two candidates", False),
    "dismiss_candidate_duplicate": ("POST", "/merchant-candidate-duplicate-dismiss", "Dismiss a possible duplicate", False),
    "list_segments": ("GET", "/merchant-segment-list", "List candidate segments", True),
    "manage_segment": ("POST", "/merchant-segment-manage", "Create, change or delete a candidate segment", False),
    "list_tasks": ("GET", "/merchant-task-list", "List tasks", True),
    "save_task": ("POST", "/merchant-task-save", "Create or change a task", False),
    "list_document_requests": ("GET", "/merchant-document-request-list", "List document requests", True),
    "request_candidate_documents": ("POST", "/merchant-document-request-create", "Ask a candidate for documents", False),
    "update_document_request": (
        "POST", "/merchant-document-request-update", "Cancel a document request or mark it reviewed", False,
    ),
}

# Hidden endpoints that are still in the API, and the tool that does their job now.
HIDDEN = {
    ("POST", "/job-interview-create-from-array"): "create_interview",
    ("POST", "/job-interview-register-users"): "invite_candidates",
    ("POST", "/catalogue-tag-update"): "save_catalogue_directory",
    ("POST", "/media-delete"): "manage_media_file",
    ("GET", "/media-folders"): "list_media_files",
    ("GET", "/report-run"): "run_report",
    ("POST", "/report-update"): "save_report",
    ("GET", "/merchant-analytics"): "run_report",
    ("GET", "/merchant-analytics-credits-used"): "run_report",
}

# Hint words that are fields of another record, named as a concept (no tool to point at).
OTHER_RECORD_FIELDS = {
    "save_catalogue_directory": {"tags"},  # a coaching session's own tags
    "list_avatars": {"interview_template_id"},  # what create_interview / create_persona take
    "list_languages": {"mojito_language_code"},  # what the create tools take
    "list_candidates": {"values", "within_days"},  # keys and operators inside a `rules` example
    "request_candidate_documents": {"form_cover_letter"},  # a value of documents[].source_field_id
    "update_position_step": {"field_type"},  # a key of a pre-screening form field
}


def _name(key: tuple[str, str]) -> str:
    """The tool name an endpoint would get: curated, or derived when it has no entry."""
    from naming import fallback_meta

    meta = TOOL_META.get(key)
    return meta.name if meta else fallback_meta(*key).name


def _annotations(tool) -> dict:
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return {}
    if isinstance(annotations, dict):
        return annotations
    return annotations.model_dump(exclude_none=True, by_alias=True)


async def _tools():
    import server

    return {t.name: t for t in await server.mcp.list_tools()}


def _words(node, out: set[str]) -> set[str]:
    """Every property name and enum value anywhere in a JSON schema."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                out.update(value)
            if key == "enum" and isinstance(value, list):
                out.update(str(v) for v in value)
            _words(value, out)
    elif isinstance(node, list):
        for value in node:
            _words(value, out)
    return out


# ---- the twenty new tools ------------------------------------------------------------------


def test_each_new_tool_is_curated_on_a_real_endpoint():
    by_name = {meta.name: key for key, meta in TOOL_META.items()}
    for name, (method, path, title, read_only) in NEW_TOOLS.items():
        assert by_name[name] == (method, path), name
        assert method.lower() in SNAPSHOT["paths"][path], f"{name}: {method} {path} is not in the snapshot"
        meta = TOOL_META[(method, path)]
        assert (meta.title, meta.read_only, meta.destructive) == (title, read_only, not read_only), name
        assert len(meta.justification) >= 40, name
        assert not is_ignored(method, path), name


@pytest.mark.asyncio
async def test_each_new_tool_is_listed_with_its_title_and_hints():
    tools = await _tools()
    for name, (_method, _path, title, read_only) in NEW_TOOLS.items():
        assert name in tools, name
        annotations = _annotations(tools[name])
        assert tools[name].title == title, name
        assert annotations["title"] == title, name
        if read_only:
            assert annotations["readOnlyHint"] is True, name
            assert annotations["destructiveHint"] is False, name
        else:
            assert annotations["readOnlyHint"] is False, name
            assert annotations["destructiveHint"] is True, name
        assert annotations["openWorldHint"] is True, name
        assert tools[name].description.strip(), name


@pytest.mark.asyncio
async def test_risky_writes_offer_a_dry_run_and_say_so():
    tools = await _tools()
    for name in ("set_position_process", "update_position_step", "manage_meeting_slot", "manage_candidate_tag", "request_candidate_documents"):
        assert "dry_run" in tools[name].parameters["properties"], name
        assert "dry_run=true" in tools[name].description, name
    merge = tools["merge_candidates"]
    assert "preview_token" in merge.parameters["properties"]
    assert "IRREVERSIBLE" in merge.description
    assert "expected_updated_at" in tools["update_position_step"].parameters["properties"]


# ---- the hidden endpoints -----------------------------------------------------------------


def test_the_merged_endpoints_are_the_ignored_ones():
    hidden = {key for key in TOOL_META if is_ignored(*key)}
    merged = set(HIDDEN)
    for key in merged:
        assert is_ignored(*key), key
        assert key[1] in SNAPSHOT["paths"], f"{key}: the endpoint stays in the API"
    # the rest were hidden before 2.0.0, for their own reasons
    older = {
        "/invite-users", "/job-interview-create-for-candidate-with-token", "/pre-screening-create",
        "/job-interview-pre-screening-api-resume-text", "/job-interview-pre-screening-api-resume-binary",
        "/merchant-document-download",
    }
    assert {path for _, path in hidden - merged} <= older
    assert older | {path for _, path in merged if (_, path) not in IGNORED_OPERATIONS} == IGNORED_PATHS


@pytest.mark.asyncio
async def test_no_hidden_endpoint_is_listed_and_its_replacement_is():
    tools = await _tools()
    for key, replacement in HIDDEN.items():
        assert _name(key) not in tools, key
        assert replacement in tools, replacement
    listed_api = {meta.name for key, meta in TOOL_META.items() if not is_ignored(*key)}
    assert listed_api <= set(tools)
    assert not ({meta.name for key, meta in TOOL_META.items() if is_ignored(*key)} & set(tools))


@pytest.mark.asyncio
async def test_a_path_shared_by_two_methods_keeps_the_one_not_ignored():
    """GET /media-folders is hidden, POST /media-folders stays manage_media_folder."""
    tools = await _tools()
    assert ("GET", "/media-folders") in IGNORED_OPERATIONS
    assert "/media-folders" not in IGNORED_PATHS
    assert "manage_media_folder" in tools
    assert "list_media_folders" not in tools
    assert "include_folders" in tools["list_media_files"].parameters["properties"]


def test_is_ignored_by_path_and_by_operation():
    # a path in IGNORED_PATHS is ignored for every method
    for method in ("GET", "POST", "post", "DELETE"):
        assert is_ignored(method, "/report-update")
        assert is_ignored(method, "/invite-users")
    # an operation is ignored only for its own method, in any case
    assert is_ignored("GET", "/media-folders")
    assert is_ignored("get", "/media-folders")
    assert not is_ignored("POST", "/media-folders")
    # nothing else
    assert not is_ignored("GET", "/merchant-candidate-get")
    assert not is_ignored("POST", "/report-create")
    assert not is_ignored("GET", "/no-such-path")


def test_justifications_skip_hidden_tools_unless_asked():
    import naming

    exposed = naming.annotation_justifications()
    everything = naming.annotation_justifications(include_ignored=True)
    for key in HIDDEN:
        if key not in TOOL_META:
            continue  # /catalogue-tag-update lost its entry: its text lives on save_catalogue_directory
        name = TOOL_META[key].name
        assert name not in exposed, name
        assert name in everything, name
    for name in NEW_TOOLS:
        assert name in exposed, name


def test_route_maps_exclude_paths_and_single_operations(monkeypatch):
    """The RouteMaps server.build_server() hands FastMCP, rebuilt the same way."""
    import server

    monkeypatch.setenv("IGNORED_TOOL_PATHS", "/merchant-task-list, ,/merchant-segment-list")
    ignored = server._ignored_paths()
    assert {"/merchant-task-list", "/merchant-segment-list"} <= ignored
    assert IGNORED_PATHS <= ignored
    assert "" not in ignored


def test_an_env_ignored_path_drops_the_tool(monkeypatch):
    import asyncio

    import server

    monkeypatch.setenv("IGNORED_TOOL_PATHS", "/merchant-task-list")
    built = server.build_server()
    names = {t.name for t in asyncio.run(built.list_tools())}
    assert "list_tasks" not in names
    assert "save_task" in names
    assert "manage_media_folder" in names and "list_media_folders" not in names


# ---- every parameter a hint names exists --------------------------------------------------


@pytest.mark.asyncio
async def test_every_parameter_a_hint_names_exists():
    """A hint naming a parameter the tool does not take sends the model into a 422.

    Checked for every exposed curated tool: `name=value` must be a parameter of the
    tool (or of the tool named just before it, as in "get_candidate include=..."),
    and every backticked or snake_case word must be a parameter, a response field or
    an enum value of this tool or of a tool the hint names.
    """
    tools = await _tools()
    vocabulary = {
        name: _words(tool.parameters, set()) | _words(tool.output_schema or {}, set()) for name, tool in tools.items()
    }
    hidden_names = {meta.name for key, meta in TOOL_META.items() if is_ignored(*key)}
    problems: list[str] = []
    checked = 0
    for key, meta in TOOL_META.items():
        if is_ignored(*key):
            continue
        hint = meta.hint
        named = [w for w in re.findall(r"\b[a-z]+(?:_[a-z0-9]+)+\b", hint) if w in tools]
        known = set(vocabulary[meta.name]).union(*(vocabulary[n] for n in named)) | OTHER_RECORD_FIELDS.get(meta.name, set())
        for hidden in hidden_names:
            if re.search(rf"\b{hidden}\b", hint):
                problems.append(f"{meta.name}: names the hidden tool {hidden}")
        for match in re.finditer(r"(?:\b(\w+)\s+(?:with\s+)?)?\b([a-z][a-z0-9_]*)=", hint):
            before, parameter = match.groups()
            owner = before if before in tools else meta.name
            checked += 1
            if parameter not in tools[owner].parameters.get("properties", {}) and parameter not in vocabulary[owner]:
                problems.append(f"{meta.name}: {owner} has no parameter {parameter}")
        for word in re.findall(r"`([a-z][a-z0-9_]*)`", hint) + re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", hint):
            checked += 1
            if word not in tools and word not in known:
                problems.append(f"{meta.name}: names {word}, which is neither a parameter nor a field")
    assert not problems, "\n".join(problems)
    assert checked > 150  # the check really ran over the hints


@pytest.mark.asyncio
async def test_the_new_hints_name_their_own_parameters():
    """The specific parameters the ATS hints steer with, so the generic check above is not vacuous."""
    tools = await _tools()
    expect = {
        "get_interview_definition": {"include"},
        "update_position_step": {"step_id", "name", "advance", "kombo", "expected_updated_at", "dry_run"},
        "manage_meeting_slot": {"action", "start_at", "duration_minutes", "capacity", "location", "meeting_url", "dry_run"},
        "duplicate_position": {"target_merchant_id"},
        "get_candidate": {"include"},
        "add_candidate_note": {"candidate_id", "result_id"},
        "set_candidate_tags": {"add_tag_ids", "remove_tag_ids"},
        "preview_candidate_merge": {"merge_id", "keep_id"},
        "merge_candidates": {"merge_id", "keep_id", "preview_token", "field_choices"},
        "dismiss_candidate_duplicate": {"duplicate_id"},
        "list_segments": {"segment_id"},
        "list_candidates": {"rules", "segment_id"},
        "save_task": {"task_id"},
        "list_document_requests": {"attention"},
        "request_candidate_documents": {"notify", "dry_run"},
        "run_report": {"report_id", "definition", "version", "section", "from", "to", "include_candidates"},
        "save_report": {"report_id", "version", "definition"},
        "save_catalogue_directory": {"action", "id", "content_md", "translations"},
        "manage_media_file": {"action", "media_id"},
        "list_media_files": {"include_folders", "folder"},
        "create_interview": {"questions", "interview_tone"},
    }
    for name, parameters in expect.items():
        missing = parameters - set(tools[name].parameters["properties"])
        assert not missing, f"{name} lacks {missing}"


@pytest.mark.asyncio
async def test_the_instructions_name_only_listed_tools_and_real_parameters():
    import server

    tools = await _tools()
    text = server.INSTRUCTIONS
    hidden_names = {meta.name for key, meta in TOOL_META.items() if is_ignored(*key)}
    for hidden in hidden_names:
        assert not re.search(rf"\b{hidden}\b", text), f"INSTRUCTIONS names the hidden tool {hidden}"
    pairs = re.findall(r"\b([a-z]+(?:_[a-z0-9]+)+)\s+(?:with\s+)?`?([a-z][a-z0-9_]*)=", text)
    assert len(pairs) >= 4
    for tool, parameter in pairs:
        assert tool in tools, tool
        assert parameter in tools[tool].parameters["properties"], f"{tool} {parameter}"
    for name in NEW_TOOLS:
        assert name in text, name


# ---- naming helpers the merged table relies on -------------------------------------------


def test_an_uncurated_root_or_blank_path_still_gets_a_name():
    from naming import fallback_meta

    root = fallback_meta("GET", "/")
    assert (root.name, root.title, root.read_only) == ("root", "/", True)
    # the hidden update endpoint has no entry of its own any more: a derived name, a write
    derived = fallback_meta("POST", "/catalogue-tag-update")
    assert (derived.name, derived.destructive) == ("catalogue_tag_update", True)


def test_narrower_view_skips_views_that_drop_no_more():
    from naming import ToolViewRules

    rules = ToolViewRules(
        parameter="view", default="summary",
        drop={"full": (), "summary": ("a",), "mid": ("a", "b"), "tiny": ("a", "b", "c")},
    )
    assert rules.narrower_than("summary") == "tiny"  # "full" drops less and is skipped
    assert rules.narrower_than("full") == "tiny"  # "mid" first, then the narrower "tiny" wins
    assert rules.narrower_than("tiny") is None
