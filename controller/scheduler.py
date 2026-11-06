"""Schedule and call-time optimization: preferred hours per person and cohort hour weights."""
from __future__ import annotations

import copy
import logging
from typing import Any, Optional

from controller.db import Database
from controller.governance import build_proposal_payload, can_auto_apply

logger = logging.getLogger("controller.scheduler")


class ScheduleOptimizer:
    def __init__(self, db: Database):
        self.db = db

    def optimize_schedule(
        self,
        workflow_id: str,
        current_sim_minute: int,
        sim_time: str,
        wf_spec: dict[str, Any],
        current_cfg_version: int,
        current_config: dict[str, Any],
    ) -> Optional[tuple[str, dict[str, Any]]]:
        """
        Extracts historical preferred connect hours for returning persons and builds
        schedule.lead_preferred_hour updates.
        """
        dial_win = wf_spec.get("dialling_window", [9, 21])
        start_h, end_h = dial_win[0], dial_win[1]

        with self.db.get_connection() as conn:
            # Query top preferred hour for each person with at least 1 connect inside the dialling window
            cur = conn.execute(
                "SELECT person_id, hour, MAX(connected_count) as max_conn "
                "FROM person_preferred_hours "
                "WHERE hour >= ? AND hour < ? "
                "GROUP BY person_id HAVING max_conn >= 1 LIMIT 50000",
                (start_h, end_h),
            )
            rows = cur.fetchall()

        if not rows:
            return None

        preferred_map = {r["person_id"]: int(r["hour"]) for r in rows}
        cur_schedule = current_config.get("schedule", {})
        cur_lph = cur_schedule.get("lead_preferred_hour", {})

        hour_of_day = (current_sim_minute % 1440) // 60
        # Only update once a day at 08:00 when new campaign batches are released, or if initial bootstrap
        if hour_of_day != 8 and cur_lph:
            return None

        # Check if we have new preferred hour entries to add
        new_entries = {k: v for k, v in preferred_map.items() if cur_lph.get(k) != v}
        if not new_entries or len(new_entries) < 50:
            return None

        new_config = copy.deepcopy(current_config)
        if "schedule" not in new_config:
            new_config["schedule"] = {}
        merged_lph = copy.deepcopy(cur_lph)
        merged_lph.update(new_entries)
        new_config["schedule"]["lead_preferred_hour"] = merged_lph

        # Ensure hour_weights covers dialling window cleanly if present
        if "hour_weights" not in new_config["schedule"]:
            new_config["schedule"]["hour_weights"] = {
                "default": {str(h): 1.0 for h in range(start_h, end_h)}
            }

        gov = wf_spec.get("governance", {})
        can_auto, reason = can_auto_apply(gov, current_config, new_config)

        if can_auto:
            logger.info(f"[{sim_time}] Auto-applying {len(new_entries)} new lead preferred hours on {workflow_id}")
            self.db.record_audit(
                current_sim_minute,
                sim_time,
                workflow_id,
                "controller_scheduler",
                "SCHEDULE_OPTIMIZED",
                f"Updated lead preferred hours for {len(new_entries)} leads",
                {"new_lead_hours_count": len(new_entries), "total_lph": len(merged_lph)},
            )
            return (
                "auto_apply",
                {
                    "proposal_id": f"sched_auto_{workflow_id}_{current_sim_minute}",
                    "base_version": current_cfg_version,
                    "proposed_config": new_config,
                    "rationale": f"Set preferred pickup hour for {len(new_entries)} returning leads based on connect history",
                },
            )

        return None

