"""Persistent SQLite database management for controller state and audit."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from controller.config import DEFAULT_DB_PATH


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

-- Ingestion state and bookmarks
CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Workflows metadata and current specs
CREATE TABLE IF NOT EXISTS workflows (
    workflow_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    spec_json TEXT NOT NULL,
    config_json TEXT NOT NULL,
    last_updated_sim_minute INTEGER NOT NULL
);

-- Daily aggregated call statistics
CREATE TABLE IF NOT EXISTS call_stats_daily (
    workflow_id TEXT NOT NULL,
    sim_day INTEGER NOT NULL,
    cohort_id TEXT NOT NULL,
    welcome_message TEXT NOT NULL,
    voice TEXT NOT NULL,
    caller_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    dials INTEGER DEFAULT 0,
    connected INTEGER DEFAULT 0,
    engaged INTEGER DEFAULT 0,
    hallucinated INTEGER DEFAULT 0,
    PRIMARY KEY (workflow_id, sim_day, cohort_id, welcome_message, voice, caller_id, prompt_version)
);

-- Hourly caller-ID stats for fast spam-flag detection
CREATE TABLE IF NOT EXISTS caller_id_hourly (
    workflow_id TEXT NOT NULL,
    sim_hour INTEGER NOT NULL,
    caller_id TEXT NOT NULL,
    dials INTEGER DEFAULT 0,
    connected INTEGER DEFAULT 0,
    PRIMARY KEY (workflow_id, sim_hour, caller_id)
);

-- Historical returning leads connect preference (person_id -> hour)
CREATE TABLE IF NOT EXISTS person_preferred_hours (
    person_id TEXT NOT NULL,
    hour INTEGER NOT NULL,
    connected_count INTEGER DEFAULT 0,
    PRIMARY KEY (person_id, hour)
);

-- Proposals submitted to simulator / governance
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    approval_id TEXT,
    workflow_id TEXT NOT NULL,
    base_version INTEGER NOT NULL,
    status TEXT NOT NULL, -- pending, approved, applied, rejected, expired, lapsed, auto_applied
    change_type TEXT NOT NULL,
    proposed_config_json TEXT NOT NULL,
    diff_json TEXT NOT NULL,
    impact_score REAL NOT NULL,
    affected_leads INTEGER NOT NULL,
    created_sim_minute INTEGER NOT NULL,
    decided_sim_minute INTEGER,
    applied_sim_minute INTEGER,
    reasons_json TEXT
);

-- Queryable audit trail of all controller actions and observations
CREATE TABLE IF NOT EXISTS audit_trail (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sim_minute INTEGER NOT NULL,
    sim_time TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    event_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_wf_minute ON audit_trail(workflow_id, sim_minute);
CREATE INDEX IF NOT EXISTS idx_call_stats_wf_day ON call_stats_daily(workflow_id, sim_day);
CREATE INDEX IF NOT EXISTS idx_cid_wf_hour ON caller_id_hourly(workflow_id, sim_hour);
"""


class Database:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._init_db()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        with self.get_connection() as conn:
            conn.executescript(SCHEMA)

    # --- Sync State Helpers ---
    def get_sync_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self.get_connection() as conn:
            cur = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,))
            row = cur.fetchone()
            return row["value"] if row else default

    def set_sync_state(self, key: str, value: str) -> None:
        with self.get_connection() as conn:
            conn.execute(
                "INSERT INTO sync_state (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
                (key, value),
            )

    # --- Audit Trail Helpers ---
    def record_audit(
        self,
        sim_minute: int,
        sim_time: str,
        workflow_id: str,
        actor: str,
        event_type: str,
        summary: str,
        details: dict[str, Any],
    ) -> None:
        with self.get_connection() as conn:
            conn.execute(
                "INSERT INTO audit_trail (sim_minute, sim_time, workflow_id, actor, event_type, summary, details_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    sim_minute,
                    sim_time,
                    workflow_id,
                    actor,
                    event_type,
                    summary,
                    json.dumps(details),
                ),
            )

    # --- Proposals Helpers ---
    def save_proposal(
        self,
        proposal_id: str,
        workflow_id: str,
        base_version: int,
        status: str,
        change_type: str,
        proposed_config: dict[str, Any],
        diff: dict[str, Any],
        impact_score: float,
        affected_leads: int,
        created_sim_minute: int,
        approval_id: Optional[str] = None,
    ) -> None:
        with self.get_connection() as conn:
            conn.execute(
                "INSERT INTO proposals (proposal_id, approval_id, workflow_id, base_version, status, change_type, "
                "proposed_config_json, diff_json, impact_score, affected_leads, created_sim_minute) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(proposal_id) DO UPDATE SET "
                "approval_id = COALESCE(excluded.approval_id, proposals.approval_id), "
                "status = excluded.status",
                (
                    proposal_id,
                    approval_id,
                    workflow_id,
                    base_version,
                    status,
                    change_type,
                    json.dumps(proposed_config),
                    json.dumps(diff),
                    impact_score,
                    affected_leads,
                    created_sim_minute,
                ),
            )

    def update_proposal_status(
        self,
        proposal_id: str,
        status: str,
        approval_id: Optional[str] = None,
        decided_sim_minute: Optional[int] = None,
        applied_sim_minute: Optional[int] = None,
        reasons: Optional[list[str]] = None,
    ) -> None:
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE proposals SET status = ?, "
                "approval_id = COALESCE(?, approval_id), "
                "decided_sim_minute = COALESCE(?, decided_sim_minute), "
                "applied_sim_minute = COALESCE(?, applied_sim_minute), "
                "reasons_json = COALESCE(?, reasons_json) "
                "WHERE proposal_id = ?",
                (
                    status,
                    approval_id,
                    decided_sim_minute,
                    applied_sim_minute,
                    json.dumps(reasons) if reasons is not None else None,
                    proposal_id,
                ),
            )

    def get_pending_proposals(self, workflow_id: Optional[str] = None) -> list[dict[str, Any]]:
        with self.get_connection() as conn:
            query = "SELECT * FROM proposals WHERE status = 'pending'"
            params = []
            if workflow_id:
                query += " AND workflow_id = ?"
                params.append(workflow_id)
            cur = conn.execute(query, params)
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def count_recent_proposals_in_24h(self, workflow_id: str, current_sim_minute: int) -> int:
        with self.get_connection() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) as cnt FROM proposals "
                "WHERE workflow_id = ? AND created_sim_minute > ? AND status != 'auto_applied'",
                (workflow_id, current_sim_minute - 1440),
            )
            row = cur.fetchone()
            return row["cnt"] if row else 0

