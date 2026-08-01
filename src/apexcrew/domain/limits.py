from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class V01MechanismLimits:
    bootstrap_tranche_calls: int = 8
    bootstrap_tranche_count: int = 2
    renewal_tranche_calls: int = 8
    task_call_ceiling: int = 48
    task_attempt_ceiling: int = 5
    stale_refresh_ceiling: int = 3
    manual_resume_ceiling: int = 2
    bootstrap_no_progress_ceiling: int = 2
    renewal_no_progress_ceiling: int = 1
    repeated_checkpoint_ceiling: int = 2
    repeated_invalid_action_ceiling: int = 3
    ordinary_action_timeout_seconds: int = 120
    check_timeout_seconds: int = 600
    provider_retry_ceiling: int = 2
    warning_percent: int = 80


V01_MECHANISM_LIMITS: Final = V01MechanismLimits()
