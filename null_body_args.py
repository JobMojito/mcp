"""Send an explicit null in a generated tool's JSON body.

FastMCP's ``RequestDirector`` leaves every ``None`` argument out of the request,
reading it as "optional and not given". For most tools that is what a model means.
For a few it is the opposite: on ``set_knowledge_base_document_interviews``, null
means "every interview" and the API requires the field, so a dropped null is a 422
and the document can never be opened again.

Opt in per endpoint with ``ToolMeta.null_body_args``. It is not the default because
models often pass null for fields they mean to skip, and on an update endpoint a
forwarded null clears the stored value. A field qualifies only when all three hold:

* the tool's description promises what null does;
* no other value reaches that state through the tool (a blank string often does);
* a stray null can be put right by sending the old value again.

The third keeps ``recording`` and ``candidate_expectations_json`` out although null is
their only off value: answers taken while proctoring is off are never recorded, and a
null rubric makes the next scored answer generate and store a new one.

Only top-level arguments are affected. A null inside an object, such as one language
in ``translations`` or one key in ``custom_fields``, reaches the API without this.

``_director`` is a private attribute of FastMCP's ``OpenAPITool``. If an upgrade
renames it this wrapper stops being called, and
``tests/test_knowledge_base_document_limit.py`` fails on the null test.
"""

from __future__ import annotations

import json
from typing import Any

import httpx


class KeepNullBodyArgs:
    """Wraps a director so the named arguments reach the JSON body when they are null."""

    def __init__(self, director: Any, names: tuple[str, ...]) -> None:
        self._director = director
        self._names = names

    def build(self, route: Any, flat_args: dict[str, Any], base_url: str = "http://localhost") -> httpx.Request:
        request = self._director.build(route, flat_args, base_url)
        nulls = {name: None for name in self._names if name in flat_args and flat_args[name] is None}
        if not nulls:
            return request
        body = json.loads(request.content) if request.content else {}
        # httpx sets these again for the new body; a stale content-length would truncate it.
        headers = {k: v for k, v in request.headers.items() if k.lower() not in {"content-length", "content-type"}}
        return httpx.Request(request.method, request.url, headers=headers, json={**body, **nulls})
