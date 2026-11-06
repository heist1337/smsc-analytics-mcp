"""Portable facade over the reviewed SMSC catalog, transport, paging and normalization."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from smsc_mcp.analysis import analyze_messages
from smsc_mcp.catalog import catalog
from smsc_mcp.client import SmscClient, validate
from smsc_mcp.history import fetch_history
from smsc_mcp.normalize import normalize
from smsc_mcp.store import ACCOUNT, Store


class Service:
    def __init__(self, store: Store, resource=None):
        self.store = store
        self._resource = resource
        self._redaction_key = store.redaction_key()

    def resource(self):
        if self._resource is not None:
            return self._resource
        configured_path = os.environ.get("SMSC_CONFIG_FILE")
        if not configured_path:
            return {
                "apikey": os.environ.get("SMSC_API_TOKEN") or os.environ.get("SMSC_API_KEY"),
                "redaction_key": os.environ.get("SMSC_REDACTION_KEY") or self._redaction_key,
                "server": os.environ.get("SMSC_SERVER", "primary"),
                "currency": os.environ.get("SMSC_CURRENCY", "unknown"),
                "timezone": os.environ.get("SMSC_TIMEZONE", "Europe/Moscow"),
                "history_currency_verified": os.environ.get("SMSC_HISTORY_CURRENCY_VERIFIED", "false").lower()
                == "true",
                "store_message_text": os.environ.get("SMSC_STORE_MESSAGE_TEXT", "false").lower() == "true",
            }
        path = Path(configured_path).expanduser()
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError("smsc_config_requires_private_file")
        resource = json.loads(path.read_text())
        if not isinstance(resource, dict):
            raise ValueError("smsc_config_object_required")
        resource.setdefault("redaction_key", self._redaction_key)
        return resource

    def client(self):
        return SmscClient(self.resource(), reserve_request=self.store.reserve_request)

    def read(self, operation="catalog", parameters=None, include_text=False, include_personal_data=False):
        if operation == "catalog":
            return {**catalog(), "documentation": "https://smsc.ru/api/http/get_data/"}
        validate(operation, parameters)
        client = self.client()
        result = client.execute(
            operation, parameters, include_text=include_text, include_personal_data=include_personal_data
        )
        self.store.save_snapshot(result, parameters or {}, client)
        return result

    def history(
        self,
        start,
        end,
        page_size=1000,
        max_pages=1,
        prev_id="",
        phone="",
        email="",
        format=0,
        include_text=False,
        include_personal_data=False,
    ):
        client = self.client()
        result = fetch_history(
            client,
            start,
            end,
            page_size=page_size,
            max_pages=max_pages,
            prev_id=prev_id,
            phone=phone,
            email=email,
            format=format,
        )
        result["rows"] = client.redact(
            result["rows"], include_text=include_text, include_personal_data=include_personal_data
        )
        return result

    def cost_report(self, start, end, max_pages=1, prev_id="", include_text=False):
        client = self.client()
        history = fetch_history(client, start, end, max_pages=max_pages, prev_id=prev_id)
        report = analyze_messages(
            history["rows"],
            currency=client.currency,
            timezone=client.timezone,
            complete=history["complete"],
            include_text=include_text,
        )
        return client.redact(
            {
                **report,
                "start": start,
                "end": end,
                "pages": history["pages"],
                "next_cursor": history["next_cursor"],
                "completeness_scope": history["completeness_scope"],
            },
            include_text=include_text,
        )

    def refresh(self, start, end, restart=False):
        """Same one-page/checkpoint/lease contract as the existing Windmill tool."""
        validate("messages", {"start": start, "end": end})
        resource = self.resource()
        client = SmscClient(resource, reserve_request=self.store.reserve_request)
        if resource.get("history_currency_verified") is not True or not re.fullmatch(r"[A-Z]{3}", str(client.currency)):
            raise ValueError("verify_history_currency_in_cabinet_before_ingestion")
        zone = ZoneInfo(client.timezone)
        if datetime.strptime(end, "%d.%m.%Y").date() >= datetime.now(zone).date():
            raise ValueError("cached_refresh_requires_closed_days; use_live_history_for_today")
        key = resource.get("redaction_key")
        if not isinstance(key, str) or len(key) < 32 or key.startswith("$var:"):
            raise ValueError("durable_ingestion_requires_separate_stable_redaction_secret")
        store_text = resource.get("store_message_text", False)
        if not isinstance(store_text, bool):
            raise ValueError("store_message_text_must_be_boolean")
        first = datetime.strptime(start, "%d.%m.%Y").date().isoformat()
        last = datetime.strptime(end, "%d.%m.%Y").date().isoformat()
        metadata = {
            "currency": client.currency,
            "timezone": client.timezone,
            "key_id": hashlib.sha256(key.encode()).hexdigest(),
            "store_message_text": store_text,
        }
        rev, previous, _ = self.store.load(f"account:{ACCOUNT}")
        if previous and any(previous.get(k) != metadata[k] for k in ("currency", "timezone", "key_id")):
            raise ValueError("account_metadata_changed; reconcile_before_ingestion")
        self.store.commit(f"account:{ACCOUNT}", rev, metadata)
        scope = f"history:{ACCOUNT}:{first}:{last}"
        revision, state, now = self.store.load(scope)
        previous_state = dict(state)
        if state.get("lease_until", 0) > now:
            raise RuntimeError("smsc_refresh_in_progress")
        if state.get("exhausted") and not restart:
            return {
                "source": "smsc_cached",
                "refresh": "already_exhausted",
                **state,
                "limitation": "API exhaustion does not prove archive retention; restart explicitly to recheck statuses.",
            }
        if not state or restart:
            state = {"start": first, "end": last, "cursor": "", "pages": 0, "rows_seen": 0, "exhausted": False}
        revision = self.store.commit(scope, revision, {**state, "lease_until": now + 120})
        try:
            page = fetch_history(client, start, end, max_pages=1, prev_id=state["cursor"])
            fetched_at = datetime.now(UTC).isoformat()
            rows = [normalize(row, client, fetched_at, store_text) for row in page["rows"]]
            self.store.save_messages(rows)
            state = {
                **state,
                "cursor": page["next_cursor"] or "",
                "pages": state["pages"] + 1,
                "rows_seen": state["rows_seen"] + len(rows),
                "exhausted": page["complete"],
                "lease_until": 0,
                "last_fetched_at": fetched_at,
            }
            self.store.commit(scope, revision, state)
        except Exception:
            try:
                self.store.commit(scope, revision, {**previous_state, "lease_until": 0})
            except RuntimeError:
                pass
            raise
        return {
            "source": "smsc_live_to_cache",
            "account": ACCOUNT,
            "rows_saved": len(rows),
            **state,
            "source_mutated": False,
            "limitation": "API exhaustion does not prove retention or final statuses.",
        }
