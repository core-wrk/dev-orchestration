"""Bounded dependency-source discovery over explicitly authorized context roots."""

import hashlib
import json
from pathlib import Path

from dev_orchestration.config.models import ProjectConfig

DEPENDENCY_SOURCE_BUDGET = 48_000
DEPENDENCY_CONTEXT_BUDGET = 24_000
_SOURCE_SUFFIXES = {".md", ".rst", ".txt", ".py", ".ts", ".tsx", ".js", ".jsx", ".json"}


def dependency_sources(repo_root: Path, config: ProjectConfig) -> tuple[str, dict]:
    """Return a bounded inventory and its source identities without leaving declared roots."""
    root = repo_root.resolve()
    authorities: dict[str, tuple[Path, bool]] = {}
    inaccessible: list[str] = []
    for name in config.context.persistent:
        lexical = root / name
        candidate = lexical.resolve()
        try:
            if candidate != lexical.absolute():
                raise ValueError
            candidate.relative_to(root)
        except ValueError:
            inaccessible.append(f"{name}: resolves outside its declared source")
            continue
        if candidate.is_file():
            # An explicitly persistent file grants authority over that file only.
            # In particular, a symlink at its declared path must not expose another
            # file, even when that target remains inside the repository.
            if candidate != lexical.absolute():
                inaccessible.append(f"{name}: resolves outside its declared source")
                continue
            authorities[candidate.relative_to(root).as_posix()] = (candidate, True)
        elif candidate.is_dir():
            inaccessible.append(f"{name}: persistent directory was not recursively enumerated")
        else:
            inaccessible.append(f"{name}: missing")
    for declared_root in config.context.on_demand.values():
        lexical = root / declared_root
        candidate = lexical.resolve()
        try:
            if candidate != lexical.absolute():
                raise ValueError
            candidate.relative_to(root)
        except ValueError:
            target_name = (
                candidate.relative_to(root).as_posix()
                if candidate.is_relative_to(root)
                else "outside repository"
            )
            inaccessible.append(
                f"{declared_root}: resolves outside its declared source ({target_name})"
            )
            continue
        if candidate.is_file():
            authorities[candidate.relative_to(root).as_posix()] = (candidate, True)
        elif candidate.is_dir():
            try:
                for path in candidate.rglob("*"):
                    if not path.is_file() or path.suffix.lower() not in _SOURCE_SUFFIXES:
                        continue
                    target = path.resolve()
                    try:
                        if target != path.absolute():
                            raise ValueError
                        target.relative_to(candidate)
                    except ValueError:
                        target_name = (
                            target.relative_to(root).as_posix()
                            if target.is_relative_to(root)
                            else "outside repository"
                        )
                        inaccessible.append(
                            f"{path.relative_to(root).as_posix()}: target outside declared root "
                            f"({target_name})"
                        )
                        continue
                    authorities[path.relative_to(root).as_posix()] = (candidate, False)
            except OSError as exc:
                inaccessible.append(f"{declared_root}: {exc}")
        else:
            inaccessible.append(f"{declared_root}: missing")

    used = 0
    truncated_any = False
    chunks: list[str] = []
    all_records: list[dict] = []
    prompt_records: list[dict] = []
    supplied_excerpts: dict[str, str] = {}
    for name in sorted(authorities):
        path = root / name
        authority, exact_file = authorities[name]
        try:
            target = path.resolve()
            if exact_file:
                if target != authority:
                    raise ValueError("target changed outside its declared file")
            else:
                target.relative_to(authority)
            raw = path.read_bytes()
            content = raw.decode("utf-8")
        except (OSError, UnicodeError, ValueError) as exc:
            inaccessible.append(f"{name}: {exc}")
            continue
        digest = hashlib.sha256(raw).hexdigest()
        identity = {
            "path": name,
            "resolved_path": target.relative_to(root).as_posix(),
            "sha256": digest,
            "bytes": len(raw),
        }
        all_records.append(identity)
        remaining = max(0, DEPENDENCY_SOURCE_BUDGET - used)
        excerpt = content.encode("utf-8")[:remaining].decode("utf-8", errors="ignore")
        used += len(excerpt.encode("utf-8"))
        truncated = len(excerpt.encode("utf-8")) < len(raw)
        truncated_any = truncated_any or truncated
        if excerpt:
            prompt_records.append({**identity, "truncated": truncated})
            chunks.append(f"--- {name} ---\n{excerpt}")
            supplied_excerpts[name] = excerpt
        if remaining == 0:
            truncated_any = truncated_any or len(all_records) < len(authorities)
    budget_exhausted = truncated_any
    status = "\n".join(
        [
            "Only the following declared prompt sources are eligible.",
            f"Inventory byte budget: {DEPENDENCY_SOURCE_BUDGET}; consumed: {used}.",
            f"Budget exhausted: {str(budget_exhausted).lower()}.",
            "Inaccessible sources: " + ("; ".join(inaccessible) if inaccessible else "none"),
            *chunks,
        ]
    )
    metadata = {
        "sources": all_records,
        "prompt_sources": prompt_records,
        "supplied_excerpts": supplied_excerpts,
        "inaccessible_sources": inaccessible,
        "declared_sources": {
            "persistent": list(config.context.persistent),
            "on_demand": dict(config.context.on_demand),
        },
        "budget_bytes": DEPENDENCY_SOURCE_BUDGET,
        "consumed_bytes": used,
        "budget_exhausted": budget_exhausted,
    }
    return status, metadata


def render_dependency_context(report: dict) -> str:
    """Render essential dependency findings without the stored source inventory."""
    warnings = {
        "inaccessible_sources": report.get("inaccessible_sources", []),
        "incomplete_searches": report.get("incomplete_searches", []),
        "budget_exhausted": report.get("budget_exhausted", False),
    }
    sections = [
        (
            "Use this report to address relevant dependencies through implementation choices, "
            "ordering, or acceptance checks. Reviewers must independently inspect cited authorized "
            "sources and search for dependencies introduced by the diff. Only evidenced "
            "incompatibilities or unavailable required prerequisites block; drafts and speculative "
            "needs are advisory, and analyst conclusions alone are not evidence. Escalate fixes "
            "outside approved scope."
        ),
        "Full immutable evidence: dependency-report.json",
        "Declared source roots: "
        + json.dumps(report.get("declared_sources", {}), ensure_ascii=False, sort_keys=True),
        "Coverage warnings: " + json.dumps(warnings, ensure_ascii=False, sort_keys=True),
        "Findings and supporting excerpts: "
        + json.dumps(report.get("findings", []), ensure_ascii=False, indent=2),
    ]
    rendered = "\n\n".join(sections)
    # Leave room for the full rendered context label, path, and separators.
    if len(rendered.encode("utf-8")) + 128 > DEPENDENCY_CONTEXT_BUDGET:
        raise ValueError(
            "dependency report essential contents exceed 24,000 UTF-8 bytes; "
            "narrow declared sources before planning"
        )
    return rendered
