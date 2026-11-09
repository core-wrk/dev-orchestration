"""Commands quoted in acceptance criteria, run as extra evidence for final verification.

A quoted command is untrusted text. It runs only when it extends a registered validation
command (same program and subcommand), passes the deny-list, contains no shell control
characters, and never through a shell. Results are evidence for the verifier, not a gate.
"""

import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from dev_orchestration.config.models import ValidationCommand, denied_tokens
from dev_orchestration.workflow.validation import TAIL_CHARS

_QUOTED = re.compile(r"`([^`]+)`")
_CONTROL = frozenset(";|&<>$`\n\r")
# Programs that run whatever follows them; they never lend a prefix.
_WRAPPERS = frozenset(
    {
        "sh", "bash", "zsh", "dash", "ksh", "fish", "env", "sudo", "doas", "xargs",
        "nohup", "time", "timeout", "nice", "exec", "eval", "command", "busybox",
    }
)  # fmt: skip
# Subcommands that run an arbitrary named program; the program becomes part of the prefix.
_RUNNERS = frozenset({"run", "exec", "x"})


@dataclass(frozen=True)
class PlannedCommand:
    text: str
    argv: list[str]
    timeout_seconds: int


def _prefix(command: str) -> tuple[str, ...] | None:
    """The program and subcommand a registered command lends, or None when it lends none."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens or PurePosixPath(tokens[0]).name in _WRAPPERS:
        return None
    if len(tokens) >= 3 and tokens[1] == "-m":
        return tuple(tokens[:3])
    if len(tokens) >= 2 and not tokens[1].startswith("-"):
        if tokens[1] in _RUNNERS:
            return tuple(tokens[:3]) if len(tokens) >= 3 else None
        return tuple(tokens[:2])
    return None


def plan_criteria_commands(
    criteria: list[str], validation: dict[str, ValidationCommand]
) -> tuple[list[PlannedCommand], list[str]]:
    """Commands to run, and a note for each quoted command that will not run."""
    registered = {spec.command.strip() for spec in validation.values()}
    prefixes = [(prefix, spec) for spec in validation.values() if (prefix := _prefix(spec.command))]
    runnable: list[PlannedCommand] = []
    refused: list[str] = []
    seen: set[str] = set()
    for criterion in criteria:
        for match in _QUOTED.finditer(criterion):
            text = match.group(1).strip()
            if not any(character.isspace() for character in text) or text in seen:
                continue  # a path or a name, not a command
            seen.add(text)
            if text in registered:
                continue  # already run as a validation command
            reason = None
            tokens: list[str] = []
            if any(character in _CONTROL for character in text):
                reason = "it contains shell control characters"
            elif denied_tokens(text):
                reason = f"it contains a denied word {sorted(denied_tokens(text))}"
            else:
                try:
                    tokens = shlex.split(text)
                except ValueError:
                    reason = "it could not be read as a command"
            matched = next(
                (spec for prefix, spec in prefixes if tuple(tokens[: len(prefix)]) == prefix),
                None,
            )
            if reason is None and matched is None:
                reason = "it does not extend a registered validation command"
            if reason is not None:
                refused.append(f"Not run: `{text}`: {reason}.")
                continue
            runnable.append(PlannedCommand(text, tokens, matched.timeout_seconds))
    return runnable, refused


def _run(command: PlannedCommand, cwd: Path) -> str:
    try:
        proc = subprocess.run(
            command.argv,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=command.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"`{command.text}`: timed out after {command.timeout_seconds}s (FAIL)"
    except OSError as exc:
        return f"`{command.text}`: could not start: {exc} (FAIL)"
    verdict = "PASS" if proc.returncode == 0 else "FAIL"
    return (
        f"`{command.text}`: exit {proc.returncode} ({verdict})\n"
        f"stdout: {proc.stdout[-TAIL_CHARS:]}\n"
        f"stderr: {proc.stderr[-TAIL_CHARS:]}"
    )


def criteria_command_evidence(
    criteria: list[str], validation: dict[str, ValidationCommand], cwd: Path
) -> str:
    """Run the eligible quoted commands; return evidence text, or "" when none were quoted."""
    runnable, refused = plan_criteria_commands(criteria, validation)
    if not runnable and not refused:
        return ""
    lines = [
        (
            "Commands named in the acceptance criteria (evidence for verification, not a "
            "validation gate):"
        ),
        *(_run(command, cwd) for command in runnable),
        *refused,
    ]
    return "\n".join(lines)
