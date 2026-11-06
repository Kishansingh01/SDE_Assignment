"""Audit trail querying and consolidated supervisor dashboard view."""
from __future__ import annotations

import argparse
import json
from typing import Any, Optional

from controller.db import Database


class AuditViewer:
    def __init__(self, db: Database):
        self.db = db

    def query_audit_trail(
        self,
        workflow_id: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self.db.get_connection() as conn:
            query = "SELECT * FROM audit_trail"
            clauses = []
            params = []
            if workflow_id:
                clauses.append("workflow_id = ?")
                params.append(workflow_id)
            if event_type:
                clauses.append("event_type = ?")
                params.append(event_type)
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY id DESC LIMIT ?"
            params.append(limit)

            cur = conn.execute(query, params)
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def get_consolidated_supervisor_view(self, workflow_id: Optional[str] = None) -> dict[str, Any]:
        """Provides a single-screen view of active tests, current config, pending requests, and recent changes."""
        with self.db.get_connection() as conn:
            # Active workflows
            cur = conn.execute("SELECT workflow_id, current_version, config_json FROM workflows")
            wfs = {r["workflow_id"]: {"version": r["current_version"], "config": json.loads(r["config_json"])} for r in cur.fetchall()}

            # Pending proposals
            cur = conn.execute("SELECT * FROM proposals WHERE status = 'pending'")
            pending = [dict(r) for r in cur.fetchall()]

            # Recent decisions
            cur = conn.execute("SELECT * FROM audit_trail ORDER BY id DESC LIMIT 10")
            recent_audit = [dict(r) for r in cur.fetchall()]

        return {
            "workflows": wfs,
            "pending_approvals_count": len(pending),
            "pending_approvals": pending,
            "recent_actions": recent_audit,
        }


def main():
    parser = argparse.ArgumentParser(description="Formi Controller Audit CLI")
    parser.add_argument("--db", default="controller.db", help="Path to SQLite database")
    parser.add_argument("--workflow", "-w", help="Filter by workflow ID")
    parser.add_argument("--type", "-t", help="Filter by event type")
    parser.add_argument("--limit", "-n", type=int, default=20, help="Max entries to return")
    parser.add_argument("--supervisor-view", "-s", action="store_true", help="Print consolidated supervisor dashboard")
    args = parser.parse_args()

    db = Database(db_path=args.db)
    viewer = AuditViewer(db)

    if args.supervisor_view:
        view = viewer.get_consolidated_supervisor_view(args.workflow)
        print("================ CONSOLIDATED SUPERVISOR DASHBOARD ================")
        print(f"Pending Approvals: {view['pending_approvals_count']}")
        for p in view["pending_approvals"]:
            print(f" - [{p['workflow_id']}] Proposal {p['proposal_id']} (v{p['base_version']}): {p['change_type']}")
        print("\nActive Workflows:")
        for wid, w in view["workflows"].items():
            print(f" - {wid}: version {w['version']}, prompt {w['config'].get('prompt_version')}")
        print("\nRecent Audit Entries:")
        for a in view["recent_actions"]:
            print(f" - [{a['sim_time']}] {a['workflow_id']} ({a['actor']}): {a['summary']}")
        print("===================================================================")
    else:
        entries = viewer.query_audit_trail(args.workflow, args.type, args.limit)
        print(f"Found {len(entries)} audit records:")
        for e in entries:
            print(f"[{e['sim_time']}] {e['workflow_id']} | {e['actor']} | {e['event_type']} -> {e['summary']}")


if __name__ == "__main__":
    main()

