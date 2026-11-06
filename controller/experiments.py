"""Experimentation engine: cohort allocation, statistical winner selection, decay detection and rollback."""
from __future__ import annotations

import copy
import logging
from typing import Any, Optional

from controller.config import (
    DECAY_DROP_THRESHOLD,
    DECAY_EVAL_WINDOW_DAYS,
    MIN_LIFT_THRESHOLD,
    MIN_SAMPLE_SIZE_CONNECTS,
    SIGNIFICANCE_ALPHA,
)
from collections import defaultdict
from controller.db import Database
from controller.governance import (
    build_dry_run,
    build_proposal_payload,
    can_auto_apply,
    is_within_approver_hours,
)
from controller.stats import passes_guardrail, two_proportion_z_test

logger = logging.getLogger("controller.experiments")


class ExperimentManager:
    def __init__(self, db: Database):
        self.db = db
        self.quarantined_variants: dict[str, set[str]] = defaultdict(set)

    def evaluate_experiments(
        self,
        workflow_id: str,
        current_sim_minute: int,
        sim_time: str,
        wf_spec: dict[str, Any],
        current_cfg_version: int,
        current_config: dict[str, Any],
    ) -> Optional[tuple[str, dict[str, Any]]]:
        """
        Evaluates variant performance for welcome_message and voice.
        Decides whether to:
        1. Explore options uniformly if unobserved.
        2. Promote statistically significant winner that satisfies guardrail.
        3. Detect decay and roll back if a winner degrades.
        Returns ("auto_apply" | "proposal", payload) or None.
        """
        gov = wf_spec.get("governance", {})
        approver_hours = gov.get("approver_hours", [10, 19])
        
        # Check approver fatigue (max_requests_per_24h)
        max_requests = gov.get("max_requests_per_24h", 3)
        recent_proposals = self.db.count_recent_proposals_in_24h(workflow_id, current_sim_minute)
        can_propose = (recent_proposals < max_requests) and is_within_approver_hours(current_sim_minute, approver_hours)

        current_day = current_sim_minute // 1440
        primary_kpi = wf_spec.get("objective", {}).get("primary", "engagement_rate")
        guardrail_spec = wf_spec.get("objective", {}).get("guardrail", {})
        guardrail_max_drop = list(guardrail_spec.values())[0] if guardrail_spec else 0.02

        # Fetch cumulative statistics per variant dimension for the default cohort
        with self.db.get_connection() as conn:
            cur = conn.execute(
                "SELECT welcome_message, voice, SUM(dials) as dials, SUM(connected) as connected, "
                "SUM(engaged) as engaged "
                "FROM call_stats_daily WHERE workflow_id = ? "
                "GROUP BY welcome_message, voice",
                (workflow_id,),
            )
            rows = cur.fetchall()

        if not rows:
            return None

        # Check options available in workflow spec
        welcome_opts = wf_spec.get("options", {}).get("welcome_message", [])
        voice_opts = wf_spec.get("options", {}).get("voice", [])

        # --- 1. Check for Decay of Current Exploit Arm ---
        decay_action = self._check_decay(
            workflow_id=workflow_id,
            current_day=current_day,
            current_config=current_config,
            current_cfg_version=current_cfg_version,
            primary_kpi=primary_kpi,
            sim_time=sim_time,
            sim_minute=current_sim_minute,
            gov=gov,
            can_propose=can_propose,
        )
        if decay_action:
            return decay_action

        # --- 2. Check for Exploration vs Winner Promotion ---
        new_config = copy.deepcopy(current_config)
        changes_made = False
        action_type = "auto_apply"
        rationale_parts = []
        lift_est = 0.0

        # Evaluate Voices
        voice_stats = {}
        for r in rows:
            v = r["voice"]
            if v not in voice_stats:
                voice_stats[v] = {"dials": 0, "connected": 0, "engaged": 0}
            voice_stats[v]["dials"] += r["dials"]
            voice_stats[v]["connected"] += r["connected"]
            voice_stats[v]["engaged"] += r["engaged"]

        tested_voices = [v for v in voice_opts if voice_stats.get(v, {}).get("connected", 0) >= MIN_SAMPLE_SIZE_CONNECTS]
        cur_voice_alloc = current_config.get("allocations", {}).get("default", {}).get("voice", {})

        # If not all voices tested with min sample size, explore untested voices
        untested_voices = [v for v in voice_opts if voice_stats.get(v, {}).get("connected", 0) < MIN_SAMPLE_SIZE_CONNECTS]
        if untested_voices and len(cur_voice_alloc) == 1:
            # Explore by allocating equal share to untested voices and current control
            explore_alloc = {v: round(1.0 / len(voice_opts), 3) for v in voice_opts}
            # Normalize
            rem = round(1.0 - sum(explore_alloc.values()), 3)
            if rem != 0 and voice_opts:
                explore_alloc[voice_opts[0]] = round(explore_alloc[voice_opts[0]] + rem, 3)
            new_config["allocations"]["default"]["voice"] = explore_alloc
            changes_made = True
            rationale_parts.append(f"Explore voice variants across {voice_opts}")
        elif len(tested_voices) >= 2:
            # We have enough sample size to run statistical hypothesis test
            control_v = voice_opts[0]
            ctrl = voice_stats.get(control_v, {"dials": 1, "connected": 1, "engaged": 0})
            ctrl_rate = ctrl["engaged"] / max(ctrl["connected"], 1)
            ctrl_pickup = ctrl["connected"] / max(ctrl["dials"], 1)

            best_v = None
            best_lift = 0.0

            for v in tested_voices:
                if v == control_v or v in self.quarantined_variants[workflow_id]:
                    continue
                cand = voice_stats[v]
                cand_rate = cand["engaged"] / max(cand["connected"], 1)
                cand_pickup = cand["connected"] / max(cand["dials"], 1)

                # Check guardrail
                if primary_kpi == "engagement_rate":
                    if not passes_guardrail(ctrl_rate, cand_rate, ctrl_pickup, cand_pickup, guardrail_max_drop):
                        continue
                    z, p_val, lift = two_proportion_z_test(ctrl["engaged"], ctrl["connected"], cand["engaged"], cand["connected"])
                else:
                    if not passes_guardrail(ctrl_pickup, cand_pickup, ctrl_rate, cand_rate, guardrail_max_drop):
                        continue
                    z, p_val, lift = two_proportion_z_test(ctrl["connected"], ctrl["dials"], cand["connected"], cand["dials"])

                # Bonferroni adjustment for multiple comparisons
                adj_alpha = SIGNIFICANCE_ALPHA / max(1, len(tested_voices) - 1)
                if p_val < adj_alpha and lift >= MIN_LIFT_THRESHOLD:
                    if lift > best_lift:
                        best_lift = lift
                        best_v = v

            if best_v and cur_voice_alloc != {best_v: 1}:
                new_config["allocations"]["default"]["voice"] = {best_v: 1}
                changes_made = True
                lift_est = max(lift_est, best_lift)
                rationale_parts.append(f"Promote winning voice {best_v} (+{best_lift:.1%} lift, p<{SIGNIFICANCE_ALPHA})")

        # Evaluate Welcome Messages
        welcome_stats = {}
        for r in rows:
            w = r["welcome_message"]
            if w not in welcome_stats:
                welcome_stats[w] = {"dials": 0, "connected": 0, "engaged": 0}
            welcome_stats[w]["dials"] += r["dials"]
            welcome_stats[w]["connected"] += r["connected"]
            welcome_stats[w]["engaged"] += r["engaged"]

        tested_welcomes = [w for w in welcome_opts if welcome_stats.get(w, {}).get("connected", 0) >= MIN_SAMPLE_SIZE_CONNECTS]
        cur_w_alloc = current_config.get("allocations", {}).get("default", {}).get("welcome_message", {})

        untested_welcomes = [w for w in welcome_opts if welcome_stats.get(w, {}).get("connected", 0) < MIN_SAMPLE_SIZE_CONNECTS]
        if untested_welcomes and len(cur_w_alloc) == 1 and not changes_made:
            explore_w = {w: round(1.0 / len(welcome_opts), 3) for w in welcome_opts}
            rem = round(1.0 - sum(explore_w.values()), 3)
            if rem != 0 and welcome_opts:
                explore_w[welcome_opts[0]] = round(explore_w[welcome_opts[0]] + rem, 3)
            new_config["allocations"]["default"]["welcome_message"] = explore_w
            changes_made = True
            rationale_parts.append(f"Explore welcome messages across {welcome_opts}")
        elif len(tested_welcomes) >= 2:
            ctrl_w = welcome_opts[0]
            ctrl = welcome_stats.get(ctrl_w, {"dials": 1, "connected": 1, "engaged": 0})
            ctrl_rate = ctrl["engaged"] / max(ctrl["connected"], 1)
            ctrl_pickup = ctrl["connected"] / max(ctrl["dials"], 1)

            best_w = None
            best_w_lift = 0.0

            for w in tested_welcomes:
                if w == ctrl_w or w in self.quarantined_variants[workflow_id]:
                    continue
                cand = welcome_stats[w]
                cand_rate = cand["engaged"] / max(cand["connected"], 1)
                cand_pickup = cand["connected"] / max(cand["dials"], 1)

                if primary_kpi == "engagement_rate":
                    if not passes_guardrail(ctrl_rate, cand_rate, ctrl_pickup, cand_pickup, guardrail_max_drop):
                        continue
                    z, p_val, lift = two_proportion_z_test(ctrl["engaged"], ctrl["connected"], cand["engaged"], cand["connected"])
                else:
                    if not passes_guardrail(ctrl_pickup, cand_pickup, ctrl_rate, cand_rate, guardrail_max_drop):
                        continue
                    z, p_val, lift = two_proportion_z_test(ctrl["connected"], ctrl["dials"], cand["connected"], cand["dials"])

                adj_alpha = SIGNIFICANCE_ALPHA / max(1, len(tested_welcomes) - 1)
                if p_val < adj_alpha and lift >= MIN_LIFT_THRESHOLD:
                    if lift > best_w_lift:
                        best_w_lift = lift
                        best_w = w

            if best_w and cur_w_alloc != {best_w: 1}:
                new_config["allocations"]["default"]["welcome_message"] = {best_w: 1}
                changes_made = True
                lift_est = max(lift_est, best_w_lift)
                rationale_parts.append(f"Promote winning welcome message {best_w} (+{best_w_lift:.1%} lift, p<{SIGNIFICANCE_ALPHA})")

        if not changes_made or new_config == current_config:
            return None

        # Route through auto_apply if allowed; otherwise check proposal window
        can_auto, auto_reason = can_auto_apply(gov, current_config, new_config)
        if can_auto:
            return (
                "auto_apply",
                {
                    "proposal_id": f"auto_{workflow_id}_{current_sim_minute}",
                    "base_version": current_cfg_version,
                    "proposed_config": new_config,
                    "rationale": "; ".join(rationale_parts),
                },
            )
        elif can_propose:
            dry_run = build_dry_run(
                affected_cohort="default",
                metric_name=primary_kpi,
                expected_baseline=0.50,
                expected_lift=lift_est or 0.05,
                sample_leads=1500,
            )
            proposal = build_proposal_payload(
                workflow_id=workflow_id,
                base_version=current_cfg_version,
                proposed_config=new_config,
                old_config=current_config,
                change_summary="; ".join(rationale_parts),
                rationale=f"Empirical cohort evaluation: {'; '.join(rationale_parts)}",
                affected_leads=1500,
                impact_score=min(1.0, max(0.1, round(lift_est * 2.0, 2))),
                dry_run=dry_run,
            )
            return ("proposal", proposal)

        return None

    def _check_decay(
        self,
        workflow_id: str,
        current_day: int,
        current_config: dict[str, Any],
        current_cfg_version: int,
        primary_kpi: str,
        sim_time: str,
        sim_minute: int,
        gov: dict[str, Any],
        can_propose: bool,
    ) -> Optional[tuple[str, dict[str, Any]]]:
        """
        Detects if an active exploit variant is suffering from performance decay.
        If current 2-day performance drops below previous window, roll back.
        """
        if current_day < 3:
            return None

        # Check welcome_message decay
        cur_w = current_config.get("allocations", {}).get("default", {}).get("welcome_message", {})
        if len(cur_w) == 1:
            active_w = list(cur_w.keys())[0]
            with self.db.get_connection() as conn:
                # Compare recent 2 days vs prior days for this welcome message
                cur = conn.execute(
                    "SELECT sim_day, SUM(dials) as dials, SUM(connected) as connected, SUM(engaged) as engaged "
                    "FROM call_stats_daily WHERE workflow_id = ? AND welcome_message = ? "
                    "GROUP BY sim_day ORDER BY sim_day DESC",
                    (workflow_id, active_w),
                )
                day_rows = {r["sim_day"]: r for r in cur.fetchall()}

            recent_engaged = sum(day_rows[d]["engaged"] for d in [current_day - 1, current_day] if d in day_rows)
            recent_connected = sum(day_rows[d]["connected"] for d in [current_day - 1, current_day] if d in day_rows)
            
            prior_engaged = sum(day_rows[d]["engaged"] for d in [current_day - 3, current_day - 2] if d in day_rows)
            prior_connected = sum(day_rows[d]["connected"] for d in [current_day - 3, current_day - 2] if d in day_rows)

            if recent_connected >= 150 and prior_connected >= 150:
                recent_rate = recent_engaged / recent_connected
                prior_rate = prior_engaged / prior_connected
                if prior_rate - recent_rate >= DECAY_DROP_THRESHOLD:
                    logger.warning(
                        f"[{sim_time}] Detected decay on {workflow_id} welcome_message {active_w}: "
                        f"rate dropped from {prior_rate:.1%} to {recent_rate:.1%}"
                    )
                    self.quarantined_variants[workflow_id].add(active_w)
                    self.db.record_audit(
                        sim_minute,
                        sim_time,
                        workflow_id,
                        "controller_detector",
                        "DECAY_DETECTED",
                        f"Decay detected on welcome_message {active_w}: {prior_rate:.1%} -> {recent_rate:.1%}",
                        {"variant": active_w, "prior_rate": prior_rate, "recent_rate": recent_rate},
                    )
                    # Roll back to baseline W1 or re-explore
                    new_config = copy.deepcopy(current_config)
                    new_config["allocations"]["default"]["welcome_message"] = {"W1": 0.5, "W2": 0.5}
                    can_auto, _ = can_auto_apply(gov, current_config, new_config)
                    if can_auto:
                        return (
                            "auto_apply",
                            {
                                "proposal_id": f"decay_rollback_{workflow_id}_{sim_minute}",
                                "base_version": current_cfg_version,
                                "proposed_config": new_config,
                                "rationale": f"Decay detected on {active_w}; roll back and re-explore",
                            },
                        )
                    elif can_propose:
                        dry_run = build_dry_run(
                            affected_cohort="default",
                            metric_name=primary_kpi,
                            expected_baseline=recent_rate,
                            expected_lift=prior_rate - recent_rate,
                            sample_leads=recent_connected,
                        )
                        proposal = build_proposal_payload(
                            workflow_id=workflow_id,
                            base_version=current_cfg_version,
                            proposed_config=new_config,
                            old_config=current_config,
                            change_summary=f"Roll back decaying welcome message {active_w}",
                            rationale=f"Performance on {active_w} dropped by {(prior_rate - recent_rate):.1%} over recent 48 hours",
                            affected_leads=1000,
                            impact_score=0.70,
                            dry_run=dry_run,
                        )
                        return ("proposal", proposal)

        return None

