"""Incident response engine: hallucination spikes, caller-ID spam-flagging detection."""
from __future__ import annotations

import copy
import logging
from typing import Any, Optional

from controller.client import SimulatorClient, SimulatorClientError
from controller.config import (
    DID_DEGRADATION_RATIO,
    DID_MIN_DIALS,
    HALLUC_MIN_CONNECTED,
    HALLUC_RECOVERY_RATE,
    HALLUC_SPIKE_RATE,
)
from controller.db import Database
from controller.governance import (
    build_dry_run,
    build_proposal_payload,
    can_auto_apply,
    detect_changed_dims,
)

logger = logging.getLogger("controller.incidents")


class IncidentManager:
    def __init__(self, client: SimulatorClient, db: Database):
        self.client = client
        self.db = db

    def check_hallucination_spike(
        self,
        workflow_id: str,
        current_sim_minute: int,
        sim_time: str,
        wf_spec: dict[str, Any],
        current_cfg_version: int,
        current_config: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """
        Monitors connected calls over the recent period to detect hallucination spikes.
        If detected, queries LLM for prompt fixes, validates safety, and builds a proposal.
        """
        current_day = current_sim_minute // 1440
        current_prompt = current_config.get("prompt_version")

        # Query connected calls and hallucination count specifically for the active prompt
        with self.db.get_connection() as conn:
            cur = conn.execute(
                "SELECT SUM(connected) as tot_connected, SUM(hallucinated) as tot_halluc "
                "FROM call_stats_daily WHERE workflow_id = ? AND prompt_version = ?",
                (workflow_id, current_prompt),
            )
            row = cur.fetchone()
            if not row or not row["tot_connected"] or row["tot_connected"] < 40:
                return None

            tot_conn = row["tot_connected"]
            tot_halluc = row["tot_halluc"] or 0
            rate = tot_halluc / tot_conn

        if rate <= HALLUC_SPIKE_RATE:
            return None

        logger.warning(
            f"[{sim_time}] Hallucination spike on {workflow_id}: "
            f"{tot_halluc}/{tot_conn} ({rate:.1%}) on prompt {current_prompt}"
        )

        self.db.record_audit(
            current_sim_minute,
            sim_time,
            workflow_id,
            "controller_detector",
            "INCIDENT_HALLUCINATION_DETECTED",
            f"Spike detected: {rate:.1%} hallucination rate on prompt {current_prompt}",
            {"connected": tot_conn, "hallucinated": tot_halluc, "rate": rate, "prompt": current_prompt},
        )

        # Call LLM suggest-fixes
        try:
            fixes = self.client.suggest_fixes(
                workflow_id,
                f"Elevated hallucination rate {rate:.1%} observed on {current_prompt}",
            )
        except SimulatorClientError as e:
            logger.error(f"Failed to query LLM suggest-fixes: {e}")
            return None

        candidates = fixes.get("suggestions", [])
        if not candidates:
            logger.warning(f"No prompt suggestions returned for {workflow_id}")
            return None

        # Filter candidates: don't re-test current prompt
        cands_filtered = [c for c in candidates if c.get("prompt_version") != current_prompt] or candidates
        # Prefer grounded / balanced prompts over overly strict compliance scripts (per Stakeholder 04 Anita Desai)
        valid_candidates = sorted(
            cands_filtered,
            key=lambda c: 1 if "strict" in c.get("summary", "").lower() or "verbatim" in c.get("summary", "").lower() else 0
        )

        # Run validation on the candidate
        gov = wf_spec.get("governance", {})
        min_pass = gov.get("min_validation_pass_rate", 0.97)

        best_val = None
        for cand in valid_candidates:
            pv = cand["prompt_version"]
            try:
                val = self.client.run_validation(workflow_id, pv, n=400)
                logger.info(f"Validation for {pv}: pass_rate={val.get('pass_rate')}")
                if val.get("pass_rate", 0.0) >= min_pass:
                    best_val = (cand, val)
                    break
            except SimulatorClientError as e:
                logger.error(f"Validation run failed for {pv}: {e}")

        if not best_val:
            logger.warning(f"No candidate passed validation threshold {min_pass} for {workflow_id}")
            return None

        cand, val = best_val
        new_prompt = cand["prompt_version"]
        new_config = copy.deepcopy(current_config)
        new_config["prompt_version"] = new_prompt

        dry_run = build_dry_run(
            affected_cohort="all",
            metric_name="hallucination_rate",
            expected_baseline=rate,
            expected_lift=-(rate - 0.005),
            sample_leads=tot_conn,
        )

        proposal = build_proposal_payload(
            workflow_id=workflow_id,
            base_version=current_cfg_version,
            proposed_config=new_config,
            old_config=current_config,
            change_summary=f"Replace {current_prompt} with grounded prompt {new_prompt} to mitigate hallucination spike",
            rationale=f"Observed hallucination rate {rate:.1%} on {current_prompt}; {new_prompt} passed validation with {val['pass_rate']:.1%}",
            affected_leads=tot_conn * 2,
            impact_score=0.90,
            dry_run=dry_run,
            evidence={"validation_run_id": val["validation_run_id"]},
        )
        return proposal

    def check_caller_id_degradation(
        self,
        workflow_id: str,
        current_sim_minute: int,
        sim_time: str,
        wf_spec: dict[str, Any],
        current_cfg_version: int,
        current_config: dict[str, Any],
    ) -> Optional[tuple[str, dict[str, Any]]]:
        """
        Monitors pickup rate across caller IDs. If a caller ID's pickup rate degrades
        significantly below peers, reallocates traffic to healthy DIDs.
        Returns ("auto_apply" | "proposal", payload) or None.
        """
        current_hour = current_sim_minute // 60
        # Query caller ID stats over the last 12 hours for rapid degradation detection
        with self.db.get_connection() as conn:
            cur = conn.execute(
                "SELECT caller_id, SUM(dials) as tot_dials, SUM(connected) as tot_conn "
                "FROM caller_id_hourly WHERE workflow_id = ? AND sim_hour >= ? "
                "GROUP BY caller_id",
                (workflow_id, max(0, current_hour - 12)),
            )
            rows = cur.fetchall()

        stats = {r["caller_id"]: (r["tot_dials"], r["tot_conn"]) for r in rows if r["tot_dials"] >= 80}
        if len(stats) < 2:
            return None

        rates = {d: conn / dials for d, (dials, conn) in stats.items()}
        best_did = max(rates, key=rates.get)
        best_rate = rates[best_did]
        if best_rate < 0.05:
            return None

        flagged_dids = [d for d, r in rates.items() if r < best_rate * DID_DEGRADATION_RATIO]
        if not flagged_dids:
            return None

        logger.warning(
            f"[{sim_time}] DID degradation on {workflow_id}: flagged DIDs={flagged_dids}, rates={rates}"
        )

        all_dids = wf_spec["options"]["caller_id"]
        healthy_dids = [d for d in all_dids if d not in flagged_dids]
        if not healthy_dids:
            healthy_dids = all_dids

        # Build new caller ID allocation
        new_config = copy.deepcopy(current_config)
        cur_cids = new_config["allocations"]["default"].get("caller_id", {})
        
        # Distribute equally among healthy DIDs
        equal_weight = round(1.0 / len(healthy_dids), 4)
        new_alloc = {d: equal_weight for d in healthy_dids}
        # Normalize sum to exactly 1.0
        rem = round(1.0 - sum(new_alloc.values()), 4)
        if rem != 0 and healthy_dids:
            new_alloc[healthy_dids[0]] = round(new_alloc[healthy_dids[0]] + rem, 4)

        if new_alloc == cur_cids:
            return None

        new_config["allocations"]["default"]["caller_id"] = new_alloc

        self.db.record_audit(
            current_sim_minute,
            sim_time,
            workflow_id,
            "controller_detector",
            "INCIDENT_DID_DEGRADATION",
            f"Flagged caller ID degradation on {flagged_dids}; reallocating to {healthy_dids}",
            {"flagged": flagged_dids, "rates": rates, "new_alloc": new_alloc},
        )

        gov = wf_spec.get("governance", {})
        auto_pol = gov.get("auto_apply", {})
        if auto_pol.get("enabled") and "caller_id" in auto_pol.get("allowed_dims", []):
            max_s = auto_pol.get("max_shift", 0.0)
            all_opts = set(cur_cids.keys()) | set(new_alloc.keys())
            full_shift = 0.5 * sum(abs(cur_cids.get(o, 0.0) - new_alloc.get(o, 0.0)) for o in all_opts)
            if full_shift > max_s and full_shift > 1e-6:
                step = (max_s - 0.005) / full_shift  # Safety margin
                stepped_alloc = {}
                for o in all_opts:
                    stepped_alloc[o] = round((1.0 - step) * cur_cids.get(o, 0.0) + step * new_alloc.get(o, 0.0), 3)
                rem = round(1.0 - sum(stepped_alloc.values()), 3)
                if rem != 0 and healthy_dids:
                    stepped_alloc[healthy_dids[0]] = round(stepped_alloc[healthy_dids[0]] + rem, 3)
                new_alloc = {k: v for k, v in stepped_alloc.items() if v > 0}
                new_config["allocations"]["default"]["caller_id"] = new_alloc

        can_auto, reason = can_auto_apply(gov, current_config, new_config)
        if can_auto:
            return (
                "auto_apply",
                {
                    "proposal_id": f"did_auto_{current_sim_minute}",
                    "base_version": current_cfg_version,
                    "proposed_config": new_config,
                    "rationale": f"Emergency rotation: rotate away from degraded caller IDs {flagged_dids}",
                },
            )
        else:
            dry_run = build_dry_run(
                affected_cohort="default",
                metric_name="pickup_rate",
                expected_baseline=rates.get(flagged_dids[0], 0.2),
                expected_lift=best_rate - rates.get(flagged_dids[0], 0.2),
                sample_leads=sum(dials for dials, _ in stats.values()),
            )
            proposal = build_proposal_payload(
                workflow_id=workflow_id,
                base_version=current_cfg_version,
                proposed_config=new_config,
                old_config=current_config,
                change_summary=f"Rotate away from degraded caller IDs {flagged_dids}",
                rationale=f"Pickup rate dropped by >25% on {flagged_dids}; shifting load to healthy DIDs {healthy_dids}",
                affected_leads=1000,
                impact_score=0.75,
                dry_run=dry_run,
            )
            return ("proposal", proposal)

