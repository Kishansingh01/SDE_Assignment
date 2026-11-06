"""Configuration and constants for the Formi Outcome-First Controller."""
import os
from pathlib import Path

# Database path
DEFAULT_DB_PATH = os.environ.get("CONTROLLER_DB_PATH", "controller.db")

# Simulation defaults
DEFAULT_ADVANCE_MINUTES = 60
ACTIVE_HOURS_ADVANCE_MINUTES = 60
INCIDENT_ADVANCE_MINUTES = 15

# Statistical test parameters
MIN_SAMPLE_SIZE_CONNECTS = 250  # Minimum connected calls per variant arm before statistical evaluation
SIGNIFICANCE_ALPHA = 0.05       # Two-sided significance level
MIN_LIFT_THRESHOLD = 0.04       # Minimum 4% relative lift to warrant promotion

# Guardrail drop threshold
DEFAULT_GUARDRAIL_MAX_DROP = 0.02

# Decay detection
DECAY_EVAL_WINDOW_DAYS = 2
DECAY_DROP_THRESHOLD = 0.06     # 6% drop below peak/control triggers rollback

# Caller ID degradation
DID_MIN_DIALS = 150
DID_DEGRADATION_RATIO = 0.75    # If pickup rate < 75% of best DID pickup rate

# Hallucination incident
HALLUC_MIN_CONNECTED = 80
HALLUC_SPIKE_RATE = 0.025       # 2.5% hallucination flag rate threshold
HALLUC_RECOVERY_RATE = 0.010    # 1.0% healthy target rate

# Approver timing
DEFAULT_APPROVER_START_HOUR = 10
DEFAULT_APPROVER_END_HOUR = 19

