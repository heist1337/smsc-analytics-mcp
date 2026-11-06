from __future__ import annotations

import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError

from smsc_mcp.server import create_server
from smsc_mcp.service import Service
from smsc_mcp.store import ACCOUNT, Store

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "synthetic-mcp-token-not-an-actual-credential"


RESOURCE = {
    "apikey": "synthetic-key",
    "currency": "RUB",
    "timezone": "Europe/Moscow",
    "redaction_key": "synthetic-stable-redaction-key-at-least-32",
    "history_currency_verified": True,
}


class SQLiteTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "smsc.sqlite3"
        self.store = Store(self.path)

    def metadata(self):
        rev, _, _ = self.store.load(f"account:{ACCOUNT}")
        self.store.commit(f"account:{ACCOUNT}", rev, {"currency": "RUB", "timezone": "Europe/Moscow"})

    def test_private_file_and_restart_persistence(self):
        rev, _, _ = self.store.load("sample")
        self.store.commit("sample", rev, {"checkpoint": "opaque"})
        self.assertEqual(Store(self.path).load("sample")[1], {"checkpoint": "opaque"})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_atomic_budget_allows_only_one_concurrent_reservation(self):
        def reserve(_):
            try:
                self.store.reserve_request("messages", 21)
                return True
            except RuntimeError:
                return False

        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(reserve, range(8))), 1)
        self.assertEqual(len(self.store.load(f"budget:{ACCOUNT}")[1]["events"]), 1)

    def test_failed_budget_prevents_transport(self):
        from smsc_mcp import client as smsc

        with (
            patch.object(self.store, "reserve_request", side_effect=RuntimeError("budget exhausted")),
            patch.object(smsc, "_post") as post,
        ):
            with self.assertRaises(RuntimeError):
                Service(self.store, RESOURCE).read("balance")
            post.assert_not_called()

    def test_costs_are_decimal_ids_unsigned_and_currency_separate(self):
        self.metadata()
        at = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp())
        rows = []
        for identifier, cost, currency in (
            ("18446744073709551615", "0.1", "RUB"),
            ("18446744073709551614", "0.2", "RUB"),
            ("9", "5.25", "USD"),
        ):
            rows.append(
                {
                    "int_id": identifier,
                    "send_timestamp": at,
                    "cost": cost,
                    "currency": currency,
                    "parts": 1,
                    "status": "1",
                    "recipient": "recipient:test",
                    "template_candidate": "family",
                    "fetched_at": "2026-10-02T00:00:00+00:00",
                }
            )
        self.store.save_messages(rows)
        result = self.store.analytics("summary", "01.10.2026", "01.10.2026")
        costs = {r["currency"]: r["known_cost"] for r in result["rows"]}
        self.assertEqual(costs, {"RUB": "0.3", "USD": "5.25"})
        self.assertFalse(result["api_scan_exhausted"])
        first = self.store.analytics("messages", "01.10.2026", "01.10.2026", limit=1)
        self.assertEqual(first["next_before_id"], "18446744073709551615")
        second = self.store.analytics(
            "messages", "01.10.2026", "01.10.2026", limit=1, before_id=first["next_before_id"]
        )
        self.assertEqual(second["rows"][0]["int_id"], "18446744073709551614")
        self.assertEqual(
            self.store.analytics("breakdown", "01.10.2026", "01.10.2026", dimensions=["day"])["rows"][0]["day"],
            "2026-10-01",
        )

    def test_newer_rows_win_and_empty_cache_is_not_zero_spending(self):
        with self.assertRaisesRegex(RuntimeError, "not_initialized"):
            self.store.analytics("summary", "01.10.2026", "01.10.2026")
        self.metadata()
        self.assertEqual(self.store.analytics("summary", "01.10.2026", "01.10.2026")["coverage"], [])
        row = {
            "int_id": "1",
            "send_timestamp": 1790812800,
            "currency": "RUB",
            "cost": "1.25",
            "fetched_at": "2026-10-03T00:00:00+00:00",
        }
        self.store.save_messages([row])
        self.store.save_messages([{**row, "cost": "9.99", "fetched_at": "2026-10-02T00:00:00+00:00"}])
        result = self.store.analytics("messages", "01.10.2026", "01.10.2026")
        self.assertEqual(result["rows"][0]["cost"], "1.25")

    def test_refresh_uses_real_sqlite_checkpoint_and_releases_failed_lease(self):
        service = Service(self.store, RESOURCE)
        with patch("smsc_mcp.service.fetch_history", return_value={"rows": [], "complete": False, "next_cursor": "50"}):
            self.assertEqual(service.refresh("01.10.2026", "01.10.2026")["cursor"], "50")
        with patch("smsc_mcp.service.fetch_history", side_effect=RuntimeError("provider unavailable")):
            with self.assertRaises(RuntimeError):
                service.refresh("01.10.2026", "01.10.2026")
        state = self.store.load(f"history:{ACCOUNT}:2026-10-01:2026-10-01")[1]
        self.assertEqual((state["cursor"], state["lease_until"]), ("50", 0))
        with patch.object(service, "resource", side_effect=AssertionError("must not load credentials")):
            self.assertFalse(self.store.analytics("coverage", "01.10.2026", "01.10.2026")["api_scan_exhausted"])

    def test_repeats_reconciliation_and_snapshots_keep_exact_cost_and_scope(self):
        self.metadata()
        at = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp())
        base = {
            "currency": "RUB",
            "parts": 1,
            "status": "1",
            "recipient": "recipient:test",
            "template_candidate": "family",
            "fetched_at": "2026-10-02T00:00:00+00:00",
            "text_length": 10,
        }
        self.store.save_messages(
            [
                {**base, "int_id": "1", "send_timestamp": at, "last_timestamp": at + 2, "cost": "0.1"},
                {**base, "int_id": "2", "send_timestamp": at + 10, "last_timestamp": at + 12, "cost": "0.2"},
            ]
        )
        repeats = self.store.analytics("repeats", "01.10.2026", "01.10.2026")
        self.assertEqual(repeats["rows"][0]["potential_repeats"], 1)
        self.assertEqual(repeats["rows"][0]["associated_known_cost"], "0.2")
        from smsc_mcp.client import SmscClient

        self.store.save_snapshot(
            {"operation": "statistics", "data": [], "fetched_at": "2026-10-02T00:00:00+00:00"},
            {"start": "01.10.2026", "end": "01.10.2026"},
            SmscClient(RESOURCE, reserve_request=Mock()),
        )
        self.assertEqual(len(self.store.analytics("snapshots", "01.10.2026", "01.10.2026")["rows"]), 1)
        reconciled = self.store.analytics("reconcile", "01.10.2026", "01.10.2026")
        self.assertEqual(reconciled["message_totals"][0]["known_cost"], "0.3")
        self.assertEqual(reconciled["message_totals"][0]["average_delivered_status_update_seconds"], "2")
        self.assertEqual(len(reconciled["statistics_observations"]), 1)


class MCPTest(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_and_offline_analytics_through_native_mcp(self):
        from smsc_mcp import client as smsc

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "smsc.sqlite3")
            service = Service(store, RESOURCE)
            row = {
                "int_id": "123",
                "send_timestamp": int(datetime(2026, 10, 1, tzinfo=UTC).timestamp()),
                "phone": "+79990001122",
                "message": "OTP 1234",
                "cost": "0.30",
                "sms_cnt": "1",
                "status": "1",
            }
            with patch.object(smsc, "_post", return_value=(json.dumps([row]).encode(), "application/json")) as post:
                async with Client(create_server(service=service)) as client:
                    result = (await client.call_tool("smsc_refresh", {"start": "01.10.2026", "end": "01.10.2026"})).data
                    self.assertTrue(result["exhausted"])
                    self.assertEqual(result["rows_saved"], 1)
                    with patch.object(
                        service, "resource", side_effect=AssertionError("offline analytics loaded credentials")
                    ):
                        report = (
                            await client.call_tool(
                                "smsc_analytics", {"operation": "summary", "start": "01.10.2026", "end": "01.10.2026"}
                            )
                        ).data
                        self.assertEqual(report["rows"][0]["known_cost"], "0.30")
                        self.assertTrue(report["api_scan_exhausted"])
                post.assert_called_once()
            with store.db() as db:
                stored = db.execute("SELECT value FROM messages").fetchone()[0]
            self.assertNotIn("+79990001122", stored)
            self.assertNotIn("OTP 1234", stored)
            self.assertEqual(Store(store.path).redaction_key(), store.redaction_key())

    async def test_smsc_real_stdio_process_without_windmill_or_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = StdioTransport(
                sys.executable,
                ["-m", "smsc_mcp"],
                cwd=str(ROOT),
                log_file=Path(directory) / "mcp.log",
                env={
                    "SMSC_DATABASE": str(Path(directory) / "smsc.sqlite3"),
                    "SMSC_CONFIG_FILE": str(Path(directory) / "missing.json"),
                },
            )
            async with Client(transport) as client:
                tools = await client.list_tools()
                self.assertEqual(
                    {t.name for t in tools},
                    {"smsc_read", "smsc_history", "smsc_cost_report", "smsc_refresh", "smsc_analytics"},
                )
                result = (await client.call_tool("smsc_read", {})).data
                self.assertIn("balance", result["operations"])
                self.assertNotIn("send", result["operations"])
                with self.assertRaises(ToolError):
                    await client.call_tool("smsc_read", {"operation": "send"})
                with self.assertRaises(ToolError):
                    await client.call_tool(
                        "smsc_analytics", {"operation": "summary", "start": "01.10.2026", "end": "01.10.2026"}
                    )
