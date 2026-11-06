"""Private SQLite state, atomic reservations and exact-decimal cached SMSC analytics."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ACCOUNT = "insurance_lm"
FIELDS = frozenset(
    {
        "operator",
        "sender",
        "status",
        "error",
        "country",
        "region",
        "mccmnc",
        "type",
        "format",
        "template_candidate",
        "recipient",
        "currency",
        "parts",
    }
)
OPERATIONS = {"summary", "breakdown", "messages", "repeats", "coverage", "snapshots", "reconcile"}


class DecimalSum:
    def __init__(self):
        self.total, self.count = Decimal(0), 0

    def step(self, value):
        if value is not None:
            self.total += Decimal(str(value))
            self.count += 1

    def finalize(self):
        return str(self.total) if self.count else None


class DecimalAverage(DecimalSum):
    def finalize(self):
        return str(self.total / self.count) if self.count else None


def bucket(timestamp, timezone, dimension):
    value = datetime.fromtimestamp(timestamp, ZoneInfo(timezone))
    return (
        str(value.isoweekday())
        if dimension == "weekday"
        else value.strftime("%Y-%m-%d %H" if dimension == "hour" else "%Y-%m-%d")
    )


def period(start, end, timezone):
    try:
        first, last = (datetime.strptime(value, "%d.%m.%Y").date() for value in (start, end))
    except ValueError:
        raise ValueError("dates_must_be_dd_mm_yyyy") from None
    if first.strftime("%d.%m.%Y") != start or last.strftime("%d.%m.%Y") != end or not 0 <= (last - first).days <= 365:
        raise ValueError("invalid_date_range; maximum_366_days")
    zone = ZoneInfo(timezone)
    return (
        int(datetime.combine(first, datetime.min.time(), zone).timestamp()),
        int(datetime.combine(last + timedelta(days=1), datetime.min.time(), zone).timestamp()),
        first.isoformat(),
        last.isoformat(),
    )


class Store:
    def __init__(self, path: Path):
        self.path = path.expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError("smsc_database_symlink_forbidden")
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(descriptor)
        except FileExistsError:
            if not self.path.is_file() or self.path.stat().st_mode & 0o077:
                raise ValueError("smsc_database_requires_private_file") from None
        with self.db() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise RuntimeError("unsupported_smsc_database_version")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS state(scope TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
                    value TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages(account TEXT NOT NULL,int_id TEXT NOT NULL,id_sort TEXT NOT NULL,
                    send_timestamp INTEGER NOT NULL,value TEXT NOT NULL,fetched_at TEXT NOT NULL,PRIMARY KEY(account,int_id));
                CREATE INDEX IF NOT EXISTS messages_period ON messages(account,send_timestamp);
                CREATE TABLE IF NOT EXISTS snapshots(snapshot_key TEXT PRIMARY KEY,operation TEXT NOT NULL,
                    parameters TEXT NOT NULL,value TEXT NOT NULL,fetched_at TEXT NOT NULL);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.create_aggregate("decimal_sum", 1, DecimalSum)
        db.create_aggregate("decimal_avg", 1, DecimalAverage)
        db.create_function("time_bucket", 3, bucket, deterministic=True)
        try:
            with db:
                yield db
        finally:
            db.close()

    def load(self, scope):
        with self.db() as db:
            db.execute("INSERT INTO state(scope,updated_at) VALUES (?,?) ON CONFLICT DO NOTHING", (scope, self.now()))
            row = db.execute("SELECT revision,value FROM state WHERE scope=?", (scope,)).fetchone()
            return row["revision"], json.loads(row["value"]), time.time()

    def redaction_key(self):
        """A stable local secret; never returned by an analytics/tool query."""
        with self.db() as db:
            db.execute(
                "INSERT INTO state(scope,value,updated_at) VALUES (?,?,?) ON CONFLICT DO NOTHING",
                (f"redaction:{ACCOUNT}", json.dumps({"key": secrets.token_urlsafe(32)}), self.now()),
            )
            return json.loads(
                db.execute("SELECT value FROM state WHERE scope=?", (f"redaction:{ACCOUNT}",)).fetchone()[0]
            )["key"]

    def commit(self, scope, revision, value):
        with self.db() as db:
            row = db.execute(
                "UPDATE state SET revision=revision+1,value=?,updated_at=? WHERE scope=? AND revision=? RETURNING revision",
                (json.dumps(value), self.now(), scope, revision),
            ).fetchone()
            if row is None:
                raise RuntimeError("smsc_concurrent_update; retry later")
            return row[0]

    @staticmethod
    def now():
        return datetime.now(UTC).isoformat()

    def reserve_request(self, operation, interval):
        # Reuse the reviewed rolling-window policy, with SQLite CAS across all processes/tools.
        from .policy import budget_state

        try:
            scope = f"budget:{ACCOUNT}"
            revision, value, now = self.load(scope)
            self.commit(scope, revision, budget_state(value, now, operation, interval))
        except Exception:
            raise RuntimeError("smsc_budget_unavailable_or_exhausted; no request sent; retry later") from None

    def save_messages(self, rows):
        with self.db() as db:
            db.executemany(
                """INSERT INTO messages VALUES (?,?,?,?,?,?) ON CONFLICT(account,int_id) DO UPDATE SET
                send_timestamp=excluded.send_timestamp,value=excluded.value,fetched_at=excluded.fetched_at
                WHERE excluded.fetched_at >= messages.fetched_at""",
                [
                    (ACCOUNT, r["int_id"], r["int_id"].zfill(20), r["send_timestamp"], json.dumps(r), r["fetched_at"])
                    for r in rows
                ],
            )

    def save_snapshot(self, result, parameters, client):
        if result["operation"] in {"invoice", "act", "download_file"}:
            return
        parameters = client.redact(parameters, operation=result["operation"])
        value = client.redact(result, operation=result["operation"])
        identifier = hashlib.sha256(json.dumps([result["operation"], parameters], sort_keys=True).encode()).hexdigest()
        fetched = datetime.fromisoformat(result["fetched_at"]).astimezone(UTC).isoformat()
        with self.db() as db:
            db.execute(
                """INSERT INTO snapshots VALUES (?,?,?,?,?) ON CONFLICT(snapshot_key) DO UPDATE SET
                value=excluded.value,fetched_at=excluded.fetched_at WHERE excluded.fetched_at >= snapshots.fetched_at""",
                (identifier, result["operation"], json.dumps(parameters), json.dumps(value), fetched),
            )

    def analytics(
        self, operation, start, end, *, dimensions=None, filters=None, limit=100, before_id="", include_text=False
    ):
        if (
            operation not in OPERATIONS
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValueError("unsupported_analytics_operation_or_limit")
        if not isinstance(include_text, bool):
            raise ValueError("include_text_must_be_boolean")
        dimensions = [] if dimensions is None else dimensions
        if (
            not isinstance(dimensions, list)
            or len(dimensions) > 3
            or any(d not in FIELDS | {"day", "hour", "weekday"} for d in dimensions)
            or len(set(dimensions)) != len(dimensions)
        ):
            raise ValueError("unsupported_dimensions; maximum_3")
        filters = {} if filters is None else filters
        if not isinstance(filters, dict) or len(filters) > 10 or filters.keys() - FIELDS:
            raise ValueError("unsupported_filters")
        if any(isinstance(v, bool) or not isinstance(v, (str, int)) or len(str(v)) > 200 for v in filters.values()):
            raise ValueError("invalid_filter_value")
        if not isinstance(before_id, str) or (before_id and (not before_id.isdigit() or len(before_id) > 20)):
            raise ValueError("invalid_before_id")
        if operation == "reconcile" and filters:
            raise ValueError("reconciliation_requires_unfiltered_period")
        with self.db() as db:
            if operation == "snapshots":
                period(start, end, "UTC")
                rows = [
                    dict(r)
                    for r in db.execute(
                        "SELECT operation,parameters,value,fetched_at FROM snapshots ORDER BY fetched_at DESC LIMIT ?",
                        (limit + 1,),
                    )
                ]
                for row in rows:
                    row.update(value=json.loads(row["value"]), parameters=json.loads(row["parameters"]))
                return {
                    "source": "smsc_cached",
                    "operation": operation,
                    "rows": rows[:limit],
                    "truncated": len(rows) > limit,
                    "snapshot_scope": "Latest saved observations for all parameters; inspect their timestamps and periods.",
                }
            metadata = db.execute("SELECT value FROM state WHERE scope=?", (f"account:{ACCOUNT}",)).fetchone()
            if metadata is None:
                raise RuntimeError("smsc_history_not_initialized; refresh_first")
            account = json.loads(metadata[0])
            begin, finish, first, last = period(start, end, account["timezone"])
            scopes = db.execute(
                """SELECT value,updated_at FROM state WHERE scope LIKE 'history:insurance_lm:%'
                AND value->>'start' <= ? AND value->>'end' >= ? ORDER BY updated_at DESC LIMIT 100""",
                (last, first),
            ).fetchall()
            evidence = [{**json.loads(r["value"]), "updated_at": r["updated_at"]} for r in scopes]
            result = {
                "source": "smsc_cached",
                "account": ACCOUNT,
                "operation": operation,
                "start": start,
                "end": end,
                "timezone": account["timezone"],
                "coverage": evidence,
                "coverage_truncated": len(evidence) >= 100,
                "api_scan_exhausted": any(
                    e.get("exhausted") and e["start"] <= first and e["end"] >= last for e in evidence
                ),
                "limitations": [
                    "Stored rows only; exhausted API scan does not prove archive retention or final statuses.",
                    "Message costs omit account charges absent from history. No conversion or proven savings inferred.",
                    "Template families and repeats are candidates; status-update latency does not prove delivery time.",
                ],
            }
            if operation == "coverage":
                return result
            params = {
                "begin": begin,
                "finish": finish,
                "account": ACCOUNT,
                "timezone": account["timezone"],
                "limit": limit + 1,
            }
            condition = "account=:account AND send_timestamp>=:begin AND send_timestamp<:finish"
            for index, (field, value) in enumerate(sorted(filters.items())):
                params[f"filter{index}"] = str(value)
                condition += f" AND CAST(value->>'{field}' AS TEXT)=:filter{index}"
            if operation == "messages":
                if before_id:
                    params["before"] = before_id.zfill(20)
                    condition += " AND id_sort<:before"
                rows = [
                    json.loads(r[0])
                    for r in db.execute(
                        f"SELECT value FROM messages WHERE {condition} ORDER BY id_sort DESC LIMIT :limit", params
                    )
                ]
                if not include_text:
                    for row in rows:
                        row.pop("message", None)
                return {
                    **result,
                    "rows": rows[:limit],
                    "has_more": len(rows) > limit,
                    "next_before_id": rows[limit - 1]["int_id"] if len(rows) > limit else None,
                }
            base = f"SELECT value,send_timestamp,fetched_at,id_sort FROM messages WHERE {condition}"
            if operation == "repeats":
                rows = db.execute(
                    f"""WITH selected AS ({base}), sequenced AS (
                    SELECT value,send_timestamp,lag(send_timestamp) OVER (PARTITION BY value->>'recipient',value->>'template_candidate'
                    ORDER BY send_timestamp,id_sort) AS previous FROM selected WHERE value->>'recipient'<>'unknown'
                    AND value->>'template_candidate'<>'unknown') SELECT value->>'recipient' AS recipient,
                    value->>'template_candidate' AS template_candidate,count(*) FILTER(WHERE send_timestamp-previous BETWEEN 0 AND 300) AS potential_repeats,
                    decimal_sum(CASE WHEN send_timestamp-previous BETWEEN 0 AND 300 THEN value->>'cost' ELSE '0' END) AS associated_known_cost
                    FROM sequenced GROUP BY 1,2 HAVING count(*) FILTER(WHERE send_timestamp-previous BETWEEN 0 AND 300)>0
                    ORDER BY potential_repeats DESC,1,2 LIMIT :limit""",
                    params,
                ).fetchall()
                return {
                    **result,
                    "rows": [dict(r) for r in rows[:limit]],
                    "truncated": len(rows) > limit,
                    "repeat_scope": "Same pseudonymous recipient/family within 5 minutes and selected period; legitimate retries may be included.",
                }
            groups = (
                ["currency", *(d for d in dimensions if d != "currency")] if operation == "breakdown" else ["currency"]
            )
            selected = ",".join(
                f"time_bucket(send_timestamp,:timezone,'{d}') AS {d}"
                if d in {"day", "hour", "weekday"}
                else f"COALESCE(value->>'{d}','unknown') AS {d}"
                for d in groups
            )
            group_by = ",".join(str(i + 1) for i in range(len(groups)))
            metrics = """count(*) AS messages,decimal_sum(value->>'cost') AS known_cost,
                count(*) FILTER(WHERE value->>'cost' IS NULL) AS missing_cost,sum(value->>'parts') AS known_parts,
                count(*) FILTER(WHERE value->>'parts' IS NULL) AS missing_parts,count(*) FILTER(WHERE value->>'parts'>1) AS multipart_messages,
                count(*) FILTER(WHERE value->>'status'='1') AS delivered_messages,count(*) FILTER(WHERE value->>'status'='0') AS pending_messages,
                count(DISTINCT NULLIF(value->>'recipient','unknown')) AS unique_recipients,decimal_avg(value->>'text_length') AS average_text_length,
                decimal_avg(CASE WHEN value->>'status'='1' AND value->>'last_timestamp'>=send_timestamp THEN (value->>'last_timestamp')-send_timestamp END) AS average_delivered_status_update_seconds,
                min(fetched_at) AS oldest_observation,max(fetched_at) AS newest_observation"""
            rows = [
                dict(r)
                for r in db.execute(
                    f"WITH selected AS ({base}) SELECT {selected},{metrics} FROM selected GROUP BY {group_by} ORDER BY messages DESC,{group_by} LIMIT :limit",
                    params,
                )
            ]
            if operation == "reconcile":
                observations = [
                    dict(r)
                    for r in db.execute(
                        "SELECT value,fetched_at FROM snapshots WHERE operation='statistics' AND parameters->>'start'=? AND parameters->>'end'=? ORDER BY fetched_at DESC LIMIT 10",
                        (start, end),
                    )
                ]
                for row in observations:
                    row["value"] = json.loads(row["value"])
                return {
                    **result,
                    "message_totals": rows,
                    "statistics_observations": observations,
                    "truncated": len(rows) > limit,
                    "reconciliation": "Compare matching currency/period/balance type; credit=expense, debit=receipts. No automatic equality assumed.",
                }
            return {
                **result,
                "rows": rows[:limit],
                "truncated": len(rows) > limit,
                "dimensions": groups,
                "currency_separated": True,
            }
