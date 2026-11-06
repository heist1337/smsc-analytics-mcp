from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier


class LocalTokenVerifier(TokenVerifier):
    def __init__(self, token: str):
        super().__init__()
        self.token = token.encode()

    async def verify_token(self, token: str) -> AccessToken | None:
        if hmac.compare_digest(token.encode(), self.token):
            return AccessToken(token=token, client_id="smsc_mcp", scopes=[])
        return None


def create_server(*, token: str | None = None, service=None) -> FastMCP:
    mcp = FastMCP(
        "smsc_mcp",
        auth=LocalTokenVerifier(token) if token else None,
        mask_error_details=True,
        instructions="Read-only towards SMSC. Never treat a page or incomplete cache as a full-period total. Check coverage, currency, timezone and continuation. Refresh/snapshots change only local state. No SMS sending or paid probes.",
    )
    from .service import Service
    from .store import Store

    if service is None:
        path = Path(os.environ.get("SMSC_DATABASE", "~/.local/share/smsc-mcp/state.sqlite3"))
        service = Service(Store(path))

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
    def smsc_read(
        operation: str = "catalog",
        parameters: dict[str, Any] | None = None,
        include_text: bool = False,
        include_personal_data: bool = False,
    ) -> dict[str, Any]:
        """Discover allowed SMSC reads without credentials using catalog. Other reads save redacted local snapshots. No sending, payments or account changes."""
        return service.read(operation, parameters, include_text, include_personal_data)

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
    def smsc_history(
        start: str,
        end: str,
        page_size: int = 1000,
        max_pages: int = 1,
        prev_id: str = "",
        phone: str = "",
        email: str = "",
        format: int = 0,
        include_text: bool = False,
        include_personal_data: bool = False,
    ) -> dict[str, Any]:
        """Read one outgoing-history page, dates DD.MM.YYYY. Follow next_cursor as prev_id until complete; this does not prove archive retention. Personal data/text hidden by default."""
        return service.history(
            start, end, page_size, max_pages, prev_id, phone, email, format, include_text, include_personal_data
        )

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
    def smsc_cost_report(
        start: str, end: str, max_pages: int = 1, prev_id: str = "", include_text: bool = False
    ) -> dict[str, Any]:
        """Analyze one live history page in its reported currency/timezone. Check complete and next_cursor; partial pages are not period totals. Message costs differ from all account charges."""
        return service.cost_report(start, end, max_pages, prev_id, include_text)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
    def smsc_refresh(start: str, end: str, restart: bool = False) -> dict[str, Any]:
        """Explicitly ingest one page into private SQLite, retaining a checkpoint. Only closed days, verified currency and stable redaction key are accepted. Never changes SMSC or schedules work."""
        return service.refresh(start, end, restart)

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False})
    def smsc_analytics(
        operation: str,
        start: str,
        end: str,
        dimensions: list[str] | None = None,
        filters: dict[str, Any] | None = None,
        limit: int = 100,
        before_id: str = "",
        include_text: bool = False,
    ) -> dict[str, Any]:
        """Analyze accumulated local history without loading API credentials. Operations: coverage, summary, breakdown, messages, repeats, snapshots, reconcile. Check coverage/truncated; empty or partial cache is not zero spending."""
        return service.store.analytics(
            operation,
            start,
            end,
            dimensions=dimensions,
            filters=filters,
            limit=limit,
            before_id=before_id,
            include_text=include_text,
        )

    return mcp
