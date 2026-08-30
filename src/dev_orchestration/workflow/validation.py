"""Run configured validation independently of provider narration."""

import subprocess
from pathlib import Path

from pydantic import BaseModel

from dev_orchestration.config.models import ValidationCommand
from dev_orchestration.domain.enums import Tier

TAIL_CHARS = 4000


class ValidationOutcome(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    name: str
    command: str
    exit_code: int | None
    passed: bool
    stdout_tail: str
    stderr_tail: str


def run_validations(
    commands: dict[str, ValidationCommand], tier: Tier, cwd: Path
) -> list[ValidationOutcome]:
    outcomes: list[ValidationOutcome] = []
    for name, spec in commands.items():
        if tier not in spec.required_for:
            continue
        try:
            proc = subprocess.run(
                ["/bin/sh", "-c", spec.command],
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=spec.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            outcomes.append(
                ValidationOutcome(
                    name=name,
                    command=spec.command,
                    exit_code=None,
                    passed=False,
                    stdout_tail="",
                    stderr_tail=f"timed out after {spec.timeout_seconds}s",
                )
            )
        except OSError as exc:
            outcomes.append(
                ValidationOutcome(
                    name=name,
                    command=spec.command,
                    exit_code=None,
                    passed=False,
                    stdout_tail="",
                    stderr_tail=str(exc),
                )
            )
        else:
            outcomes.append(
                ValidationOutcome(
                    name=name,
                    command=spec.command,
                    exit_code=proc.returncode,
                    passed=proc.returncode == 0,
                    stdout_tail=proc.stdout[-TAIL_CHARS:],
                    stderr_tail=proc.stderr[-TAIL_CHARS:],
                )
            )
    return outcomes


def all_passed(outcomes: list[ValidationOutcome]) -> bool:
    return all(outcome.passed for outcome in outcomes)
