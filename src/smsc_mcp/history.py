from __future__ import annotations

from typing import Any

from .client import SmscClient, SmscError, integer, validate


def fetch_history(
    client: SmscClient,
    start: str,
    end: str,
    *,
    page_size: int = 1000,
    max_pages: int = 1,
    prev_id: str = "",
    phone: str = "",
    email: str = "",
    format: int = 0,
) -> dict[str, Any]:
    page_size = integer("page_size", page_size, 1, 1000)
    max_pages = integer("max_pages", max_pages, 1, 1)
    parameters = {"start": start, "end": end, "cnt": page_size, "format": format}
    for key, value in (("prev_id", prev_id), ("phone", phone), ("email", email)):
        if value:
            parameters[key] = value
    validate("messages", parameters)
    rows, seen, cursors = [], set(), set()
    pages = 0
    complete = False
    next_cursor = prev_id or None
    for _ in range(max_pages):
        result = client.execute("messages", parameters, include_text=True, include_personal_data=True)
        batch = result["data"]
        if not isinstance(batch, list) or len(batch) > page_size or any(not isinstance(row, dict) for row in batch):
            raise SmscError("smsc_history_unexpected_shape")
        pages += 1
        for row in batch:
            identifier = str(row.get("int_id", ""))
            if not identifier or not identifier.isdigit():
                raise SmscError("smsc_history_missing_global_id")
            if identifier not in seen:
                rows.append(row)
                seen.add(identifier)
        cursor = str(min(int(row["int_id"]) for row in batch)) if batch else None
        if cursor and (cursor in cursors or (parameters.get("prev_id") and int(cursor) >= int(parameters["prev_id"]))):
            raise SmscError("smsc_history_cursor_did_not_advance")
        if len(batch) < page_size:
            complete = True
            next_cursor = None
            break
        # Global IDs are server-assigned and numeric. Continue strictly backward.
        cursors.add(cursor)
        next_cursor = cursor
        parameters["prev_id"] = cursor
    return {
        "rows": rows,
        "pages": pages,
        "complete": complete,
        "next_cursor": next_cursor,
        "start": start,
        "end": end,
        "page_size": page_size,
        "completeness_scope": "Filtered period from supplied cursor; does not prove archive retention.",
        "limits": "Whole SMSC account history budget <=30 requests/hour; coordinate with other LM consumers.",
    }
