"""Event ingestion pipeline: streams NDJSON chunks and updates online rollups."""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any, Callable, Optional

from controller.client import SimulatorClient
from controller.db import Database

logger = logging.getLogger("controller.ingest")


class EventIngestor:
    def __init__(self, client: SimulatorClient, db: Database):
        self.client = client
        self.db = db

    def ingest_chunk(
        self,
        chunk_info: dict[str, Any],
        on_supervisor_edit: Optional[Callable[[str, int, dict[str, Any]], None]] = None,
        on_approval_decided: Optional[Callable[[str, str, list[str]], None]] = None,
    ) -> int:
        """
        Streams and parses one NDJSON chunk. Aggregates call stats in memory,
        then commits batch updates in a single SQLite transaction.
        """
        chunk_no = chunk_info["chunk"]
        
        # In-memory batch aggregators
        # key: (workflow_id, sim_day, cohort_id, welcome, voice, caller_id, prompt) -> [dials, connected, engaged, halluc]
        call_rollups = defaultdict(lambda: [0, 0, 0, 0])
        # key: (workflow_id, sim_hour, caller_id) -> [dials, connected]
        cid_rollups = defaultdict(lambda: [0, 0])
        # key: (person_id, hour) -> connected_count
        person_hour_counts = defaultdict(int)

        event_count = 0
        supervisor_edits = []
        decisions = []

        for line in self.client.get_chunk_stream(chunk_no):
            line = line.strip()
            if not line:
                continue
            event_count += 1
            try:
                ev = json.loads(line)
            except Exception:
                continue

            ev_type = ev.get("type")

            if ev_type == "call":
                wid = ev.get("workflow_id")
                ts = ev.get("ts", "")
                # Extract day and hour from ts or event_id
                # ISO format: 2027-01-04T09:15:00+05:30
                try:
                    # hour in [0..23]
                    hour = int(ts[11:13])
                except Exception:
                    hour = 9

                # Approximate sim_day from date string or event sequence
                # Day starts at 2027-01-04
                try:
                    day_num = int(ts[8:10]) - 4
                except Exception:
                    day_num = 0

                cohort_id = ev.get("cohort_id", "default")
                var = ev.get("variant", {})
                w_msg = var.get("welcome_message", "W1")
                voice = var.get("voice", "V1")
                cid = var.get("caller_id", "D1")
                pv = var.get("prompt_version", "P1")

                connected = 1 if ev.get("connected") else 0
                engaged = 1 if ev.get("engaged") else 0
                qa = ev.get("qa") or {}
                halluc = 1 if qa.get("hallucination_flag") else 0

                # Update call rollups
                r = call_rollups[(wid, day_num, cohort_id, w_msg, voice, cid, pv)]
                r[0] += 1
                r[1] += connected
                r[2] += engaged
                r[3] += halluc

                # Update caller ID hourly rollups
                sim_hour = day_num * 24 + hour
                cr = cid_rollups[(wid, sim_hour, cid)]
                cr[0] += 1
                cr[1] += connected

                # Update returning lead connect hour preference
                person_id = ev.get("person_id")
                if connected and person_id:
                    person_hour_counts[(person_id, hour)] += 1

            elif ev_type == "config_changed":
                wid = ev.get("workflow_id")
                version = ev.get("version")
                actor = ev.get("actor")
                if actor == "supervisor":
                    supervisor_edits.append((wid, version, ev))

            elif ev_type == "approval_decided":
                aid = ev.get("approval_id")
                status = ev.get("status")
                reasons = ev.get("reasons", [])
                decisions.append((aid, status, reasons))

        # Commit batch rollups to SQLite in a single transaction
        with self.db.get_connection() as conn:
            # 1. Update call_stats_daily
            conn.executemany(
                "INSERT INTO call_stats_daily (workflow_id, sim_day, cohort_id, welcome_message, voice, caller_id, "
                "prompt_version, dials, connected, engaged, hallucinated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(workflow_id, sim_day, cohort_id, welcome_message, voice, caller_id, prompt_version) DO UPDATE SET "
                "dials = dials + excluded.dials, "
                "connected = connected + excluded.connected, "
                "engaged = engaged + excluded.engaged, "
                "hallucinated = hallucinated + excluded.hallucinated",
                [
                    (k[0], k[1], k[2], k[3], k[4], k[5], k[6], v[0], v[1], v[2], v[3])
                    for k, v in call_rollups.items()
                ],
            )

            # 2. Update caller_id_hourly
            conn.executemany(
                "INSERT INTO caller_id_hourly (workflow_id, sim_hour, caller_id, dials, connected) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(workflow_id, sim_hour, caller_id) DO UPDATE SET "
                "dials = dials + excluded.dials, "
                "connected = connected + excluded.connected",
                [
                    (k[0], k[1], k[2], v[0], v[1])
                    for k, v in cid_rollups.items()
                ],
            )

            # 3. Update person_preferred_hours
            conn.executemany(
                "INSERT INTO person_preferred_hours (person_id, hour, connected_count) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(person_id, hour) DO UPDATE SET "
                "connected_count = connected_count + excluded.connected_count",
                [
                    (k[0], k[1], v)
                    for k, v in person_hour_counts.items()
                ],
            )

            # 4. Update sync bookmark
            conn.execute(
                "INSERT INTO sync_state (key, value, updated_at) VALUES ('last_chunk', ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
                (str(chunk_no),),
            )

        # Notify callbacks
        for wid, ver, ev in supervisor_edits:
            if on_supervisor_edit:
                on_supervisor_edit(wid, ver, ev)

        for aid, st, rsn in decisions:
            if on_approval_decided:
                on_approval_decided(aid, st, rsn)

        return event_count

