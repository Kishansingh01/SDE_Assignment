"""Main controller loop: orchestrates ingestion, detection, governance, and advancement."""
from __future__ import annotations

import argparse
import copy
import json
import logging
import sys
import time
from typing import Any, Optional

from controller.client import SimulatorClient, SimulatorClientError
from controller.config import (
    DEFAULT_ADVANCE_MINUTES,
    DEFAULT_DB_PATH,
    INCIDENT_ADVANCE_MINUTES,
)
from controller.db import Database
from controller.experiments import ExperimentManager
from controller.incidents import IncidentManager
from controller.ingest import EventIngestor
from controller.scheduler import ScheduleOptimizer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("controller.loop")


class ControllerLoop:
    def __init__(self, sim_url: str, db_path: str = DEFAULT_DB_PATH):
        self.sim_url = sim_url
        self.client = SimulatorClient(base_url=sim_url)
        self.db = Database(db_path=db_path)
        self.ingestor = EventIngestor(self.client, self.db)
        self.incidents = IncidentManager(self.client, self.db)
        self.experiments = ExperimentManager(self.db)
        self.scheduler = ScheduleOptimizer(self.db)

        # In-memory cache of workflow specs and configurations
        self.workflows: dict[str, dict[str, Any]] = {}
        self.current_configs: dict[str, dict[str, Any]] = {}
        self.pending_approvals: dict[str, str] = {}  # workflow_id -> approval_id

    def bootstrap(self) -> None:
        """Initializes workflows from API and ingests pre-D0 history chunks if not already processed."""
        logger.info(f"Connecting to simulator at {self.sim_url}")
        wfs = self.client.get_workflows()
        for w in wfs:
            wid = w["workflow_id"]
            self.workflows[wid] = w
            cfg_resp = self.client.get_workflow_config(wid)
            self.current_configs[wid] = cfg_resp["config"]
            
            with self.db.get_connection() as conn:
                conn.execute(
                    "INSERT INTO workflows (workflow_id, tenant_id, current_version, spec_json, config_json, last_updated_sim_minute) "
                    "VALUES (?, ?, ?, ?, ?, 0) "
                    "ON CONFLICT(workflow_id) DO UPDATE SET "
                    "current_version = excluded.current_version, "
                    "spec_json = excluded.spec_json, "
                    "config_json = excluded.config_json",
                    (
                        wid,
                        w["tenant_id"],
                        cfg_resp["version"],
                        json.dumps(w),
                        json.dumps(cfg_resp["config"]),
                    ),
                )

        # Check last ingested chunk
        last_chunk_str = self.db.get_sync_state("last_chunk")
        last_chunk = int(last_chunk_str) if last_chunk_str is not None else -1

        # Reconcile pending approvals after crash/restart
        for p in self.db.get_pending_proposals():
            if p.get("approval_id"):
                self.pending_approvals[p["workflow_id"]] = p["approval_id"]

        # Ingest history chunks if starting fresh
        if last_chunk < 0:
            history_chunks = self.client.list_chunks(kind="history", after=-1)
            logger.info(f"Ingesting {len(history_chunks)} historical pre-D0 chunks...")
            for ch in history_chunks:
                self.ingestor.ingest_chunk(ch)

    def on_supervisor_edit(self, workflow_id: str, version: int, event: dict[str, Any]) -> None:
        """Handles supervisor edit mid-experiment without overwriting."""
        logger.warning(f"Supervisor edit detected on {workflow_id} -> v{version}")
        # Refresh current config from simulator
        try:
            cur = self.client.get_workflow_config(workflow_id)
            self.current_configs[workflow_id] = cur["config"]
            with self.db.get_connection() as conn:
                conn.execute(
                    "UPDATE workflows SET current_version = ?, config_json = ? WHERE workflow_id = ?",
                    (version, json.dumps(cur["config"]), workflow_id),
                )
            self.db.record_audit(
                event.get("event_id", 0),
                event.get("ts", ""),
                workflow_id,
                "supervisor",
                "SUPERVISOR_EDIT_PRESERVED",
                f"Supervisor updated config to version {version}",
                event,
            )
            # Invalidate any pending approval that was based on the old version
            if workflow_id in self.pending_approvals:
                self.pending_approvals.pop(workflow_id, None)
        except Exception as e:
            logger.error(f"Error handling supervisor edit: {e}")

    def on_approval_decided(self, approval_id: str, status: str, reasons: list[str]) -> None:
        logger.info(f"Approval {approval_id} decided: {status} (reasons: {reasons})")
        with self.db.get_connection() as conn:
            cur = conn.execute("SELECT workflow_id, proposal_id FROM proposals WHERE approval_id = ?", (approval_id,))
            row = cur.fetchone()
            if row:
                self.db.update_proposal_status(
                    proposal_id=row["proposal_id"],
                    status=status,
                    approval_id=approval_id,
                    reasons=reasons,
                )

    def reconcile_approvals(self, sim_minute: int, sim_time: str) -> None:
        """Polls and applies approved requests within the apply window."""
        for wid, aid in list(self.pending_approvals.items()):
            try:
                ap = self.client.get_approval(aid)
                status = ap.get("status")
                if status == "approved":
                    # Apply approved proposal
                    logger.info(f"[{sim_time}] Applying approved config for {wid} (approval {aid})")
                    res = self.client.apply_approval(wid, aid)
                    new_ver = res.get("version")
                    # Update cache
                    cur = self.client.get_workflow_config(wid)
                    self.current_configs[wid] = cur["config"]
                    self.pending_approvals.pop(wid, None)

                    self.db.update_proposal_status(
                        proposal_id=ap.get("proposal_id", ""),
                        status="applied",
                        approval_id=aid,
                        applied_sim_minute=sim_minute,
                    )
                    self.db.record_audit(
                        sim_minute,
                        sim_time,
                        wid,
                        "controller_approved",
                        "APPROVAL_APPLIED",
                        f"Applied approved proposal v{new_ver} (dims: {res.get('changed_dims')})",
                        res,
                    )
                elif status in ("rejected", "expired", "lapsed"):
                    logger.info(f"[{sim_time}] Approval {aid} for {wid} {status}: {ap.get('reasons')}")
                    self.pending_approvals.pop(wid, None)
                    self.db.update_proposal_status(
                        proposal_id=ap.get("proposal_id", ""),
                        status=status,
                        approval_id=aid,
                        decided_sim_minute=sim_minute,
                        reasons=ap.get("reasons", []),
                    )
            except SimulatorClientError as e:
                if e.code == "stale_base_version":
                    logger.warning(f"Apply failed due to stale base version for {wid}: {e.detail}")
                    self.pending_approvals.pop(wid, None)
                elif e.code == "approval_lapsed":
                    logger.warning(f"Approval {aid} lapsed for {wid}")
                    self.pending_approvals.pop(wid, None)
                else:
                    logger.error(f"Error checking/applying approval {aid}: {e}")

    def run(self) -> None:
        """Runs the controller continuously until the simulation completes."""
        self.bootstrap()
        logger.info("Starting controller event loop...")

        advance_step = DEFAULT_ADVANCE_MINUTES

        while True:
            # Advance simulation clock
            try:
                state = self.client.advance(minutes=advance_step)
            except SimulatorClientError as e:
                if e.status_code == 409 and "finished" in str(e.detail):
                    logger.info("Simulation finished (409 finished).")
                    break
                logger.error(f"Error advancing simulator: {e}")
                time.sleep(1)
                continue

            sim_time = state.get("ts", "")
            sim_minute = state.get("sim_minute", 0)
            new_chunks = state.get("new_chunks", [])
            credits_remaining = state.get("credits_remaining", 0)

            # Ingest all new NDJSON chunks
            for ch in new_chunks:
                self.ingestor.ingest_chunk(
                    ch,
                    on_supervisor_edit=self.on_supervisor_edit,
                    on_approval_decided=self.on_approval_decided,
                )

            # Reconcile pending approvals
            self.reconcile_approvals(sim_minute, sim_time)

            # Periodic decision making across workflows
            for wid, spec in self.workflows.items():
                cur_cfg = self.current_configs.get(wid)
                if not cur_cfg:
                    continue

                # Get current version from simulator
                cur_info = self.client.get_workflow_config(wid)
                cur_ver = cur_info["version"]
                self.current_configs[wid] = cur_info["config"]

                # --- 1. Incident Detection (Hallucination spike) ---
                if wid not in self.pending_approvals:
                    halluc_prop = self.incidents.check_hallucination_spike(
                        workflow_id=wid,
                        current_sim_minute=sim_minute,
                        sim_time=sim_time,
                        wf_spec=spec,
                        current_cfg_version=cur_ver,
                        current_config=cur_info["config"],
                    )
                    if halluc_prop:
                        try:
                            logger.info(f"[{sim_time}] Submitting emergency hallucination fix for {wid}")
                            resp = self.client.request_approval(halluc_prop)
                            aid = resp["approval_id"]
                            self.pending_approvals[wid] = aid
                            self.db.save_proposal(
                                proposal_id=halluc_prop["proposal_id"],
                                workflow_id=wid,
                                base_version=cur_ver,
                                status="pending",
                                change_type="hallucination_incident_fix",
                                proposed_config=halluc_prop["proposed_config"],
                                diff=halluc_prop["diff"],
                                impact_score=halluc_prop["impact_score"],
                                affected_leads=halluc_prop["affected_leads"],
                                created_sim_minute=sim_minute,
                                approval_id=aid,
                            )
                            self.db.record_audit(
                                sim_minute,
                                sim_time,
                                wid,
                                "controller",
                                "PROPOSAL_REQUESTED",
                                f"Requested approval {aid} for prompt fix {halluc_prop['proposed_config']['prompt_version']}",
                                halluc_prop,
                            )
                            continue
                        except SimulatorClientError as e:
                            logger.error(f"Failed to submit hallucination proposal: {e}")

                # --- 2. Caller ID Degradation ---
                cid_action = self.incidents.check_caller_id_degradation(
                    workflow_id=wid,
                    current_sim_minute=sim_minute,
                    sim_time=sim_time,
                    wf_spec=spec,
                    current_cfg_version=cur_ver,
                    current_config=cur_info["config"],
                )
                if cid_action:
                    act_type, payload = cid_action
                    if act_type == "auto_apply":
                        try:
                            res = self.client.auto_apply_config(wid, payload)
                            logger.info(f"[{sim_time}] Auto-applied caller ID rotation for {wid}: {res}")
                            cur_info = self.client.get_workflow_config(wid)
                            self.current_configs[wid] = cur_info["config"]
                            self.db.record_audit(
                                sim_minute,
                                sim_time,
                                wid,
                                "controller_auto",
                                "AUTO_APPLIED",
                                f"Auto-applied caller ID rotation to v{res.get('version')}",
                                res,
                            )
                            continue
                        except SimulatorClientError as e:
                            logger.error(f"Failed to auto-apply caller ID rotation: {e}")
                    elif act_type == "proposal" and wid not in self.pending_approvals:
                            try:
                                resp = self.client.request_approval(payload)
                                aid = resp["approval_id"]
                                self.pending_approvals[wid] = aid
                                self.db.save_proposal(
                                    proposal_id=payload["proposal_id"],
                                    workflow_id=wid,
                                    base_version=cur_ver,
                                    status="pending",
                                    change_type="caller_id_rotation",
                                    proposed_config=payload["proposed_config"],
                                    diff=payload["diff"],
                                    impact_score=payload["impact_score"],
                                    affected_leads=payload["affected_leads"],
                                    created_sim_minute=sim_minute,
                                    approval_id=aid,
                                )
                                continue
                            except SimulatorClientError as e:
                                logger.error(f"Failed to submit caller ID proposal: {e}")

                # --- 3. Call-time / Schedule Optimization ---
                sched_action = self.scheduler.optimize_schedule(
                    workflow_id=wid,
                    current_sim_minute=sim_minute,
                    sim_time=sim_time,
                    wf_spec=spec,
                    current_cfg_version=cur_ver,
                    current_config=cur_info["config"],
                )
                if sched_action:
                    act_type, payload = sched_action
                    if act_type == "auto_apply":
                        try:
                            res = self.client.auto_apply_config(wid, payload)
                            logger.info(f"[{sim_time}] Auto-applied schedule optimization for {wid}: {res}")
                            cur_info = self.client.get_workflow_config(wid)
                            self.current_configs[wid] = cur_info["config"]
                        except SimulatorClientError as e:
                            logger.error(f"Failed to auto-apply schedule: {e}")

                # --- 4. Variant Experimentation (Welcome message & Voice) ---
                if wid not in self.pending_approvals:
                    exp_action = self.experiments.evaluate_experiments(
                        workflow_id=wid,
                        current_sim_minute=sim_minute,
                        sim_time=sim_time,
                        wf_spec=spec,
                        current_cfg_version=cur_ver,
                        current_config=cur_info["config"],
                    )
                    if exp_action:
                        act_type, payload = exp_action
                        if act_type == "auto_apply":
                            try:
                                res = self.client.auto_apply_config(wid, payload)
                                logger.info(f"[{sim_time}] Auto-applied variant optimization for {wid}: {res}")
                                cur_info = self.client.get_workflow_config(wid)
                                self.current_configs[wid] = cur_info["config"]
                                self.db.record_audit(
                                    sim_minute,
                                    sim_time,
                                    wid,
                                    "controller_auto",
                                    "AUTO_APPLIED",
                                    f"Auto-applied variant change to v{res.get('version')}",
                                    res,
                                )
                            except SimulatorClientError as e:
                                logger.error(f"Failed to auto-apply variant change: {e}")
                        elif act_type == "proposal":
                            try:
                                resp = self.client.request_approval(payload)
                                aid = resp["approval_id"]
                                self.pending_approvals[wid] = aid
                                self.db.save_proposal(
                                    proposal_id=payload["proposal_id"],
                                    workflow_id=wid,
                                    base_version=cur_ver,
                                    status="pending",
                                    change_type="variant_experiment",
                                    proposed_config=payload["proposed_config"],
                                    diff=payload["diff"],
                                    impact_score=payload["impact_score"],
                                    affected_leads=payload["affected_leads"],
                                    created_sim_minute=sim_minute,
                                    approval_id=aid,
                                )
                                self.db.record_audit(
                                    sim_minute,
                                    sim_time,
                                    wid,
                                    "controller",
                                    "PROPOSAL_REQUESTED",
                                    f"Requested approval {aid} for {payload.get('change_summary')}",
                                    payload,
                                )
                            except SimulatorClientError as e:
                                logger.error(f"Failed to request variant approval: {e}")

            if state.get("finished"):
                logger.info("Simulation completed.")
                break

        final_st = self.client.get_state()
        logger.info(f"Final Simulator State: {final_st}")


def main():
    parser = argparse.ArgumentParser(description="Formi Outcome-First Controller")
    parser.add_argument("--url", default="http://127.0.0.1:8700", help="Simulator API URL")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to SQLite state database")
    args = parser.parse_args()

    controller = ControllerLoop(sim_url=args.url, db_path=args.db)
    controller.run()


if __name__ == "__main__":
    main()

