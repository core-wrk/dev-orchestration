import pytest
import yaml

from dev_orchestration.config.models import ProjectConfig
from dev_orchestration.init_repo import FileExistsRefusal, initialize_repo, render_project_yaml

CONFIG = ProjectConfig.model_validate(
    {
        "project": {"name": "helmfast-site", "class": "marketing_website"},
        "scope": {"include": ["src/", "docs/"], "exclude": ["node_modules/"]},
        "validation": {"build": {"command": "npm run build"}},
    }
)


def test_initialize_writes_the_repository_contract(tmp_path):
    written = initialize_repo(tmp_path, CONFIG)
    names = {p.relative_to(tmp_path).as_posix() for p in written}
    assert names == {"AGENTS.md", ".ai/project.yaml", ".ai/context.md"}
    assert (tmp_path / ".ai" / "project.yaml").is_file()


def test_written_config_round_trips(tmp_path):
    initialize_repo(tmp_path, CONFIG)
    loaded = yaml.safe_load((tmp_path / ".ai" / "project.yaml").read_text())
    restored = ProjectConfig.model_validate(loaded)
    assert restored.project.name == "helmfast-site"
    assert restored.scope.exclude == ["node_modules/"]


def test_initialize_refuses_to_overwrite(tmp_path):
    (tmp_path / "AGENTS.md").write_text("existing content\n")
    with pytest.raises(FileExistsRefusal) as excinfo:
        initialize_repo(tmp_path, CONFIG)
    assert "AGENTS.md" in str(excinfo.value)
    assert (tmp_path / "AGENTS.md").read_text() == "existing content\n"


def test_initialize_refuses_to_overwrite_existing_context_md(tmp_path):
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "context.md").write_text("hand-authored notes\n")
    with pytest.raises(FileExistsRefusal) as excinfo:
        initialize_repo(tmp_path, CONFIG)
    assert "context.md" in str(excinfo.value)
    assert (tmp_path / ".ai" / "context.md").read_text() == "hand-authored notes\n"


def test_force_allows_regeneration(tmp_path):
    # --force regenerates AGENTS.md and .ai/project.yaml (both rendered wholly
    # from config, so nothing is lost by rewriting them). It never regenerates
    # .ai/context.md, so a second, forced call only reports two written paths.
    initialize_repo(tmp_path, CONFIG)
    written = initialize_repo(tmp_path, CONFIG, force=True)
    names = {p.relative_to(tmp_path).as_posix() for p in written}
    assert names == {"AGENTS.md", ".ai/project.yaml"}
    assert len(written) == 2


def test_force_never_regenerates_context_md(tmp_path):
    # .ai/context.md holds onboarding facts that cannot be recovered by
    # reading the repository. Regenerating it under --force would be silent
    # data loss, so it must survive byte-for-byte.
    initialize_repo(tmp_path, CONFIG)
    context_path = tmp_path / ".ai" / "context.md"
    context_path.write_text("Hand-edited: staging DB requires VPN.\n")

    initialize_repo(tmp_path, CONFIG, force=True)

    assert context_path.read_text() == "Hand-edited: staging DB requires VPN.\n"


def test_render_project_yaml_round_trips_to_an_equal_config():
    # Watches the `class` alias on ProjectMeta: dumping by_alias and reloading
    # must reconstruct a config equal to the one rendered, not merely one with
    # a matching name/exclude field.
    restored = ProjectConfig.model_validate(yaml.safe_load(render_project_yaml(CONFIG)))
    assert restored == CONFIG


def test_agents_md_states_the_scope_fence(tmp_path):
    initialize_repo(tmp_path, CONFIG)
    text = (tmp_path / "AGENTS.md").read_text()
    assert "node_modules/" in text
    assert "npm run build" in text


def test_agents_md_places_include_and_exclude_in_the_correct_scope_side(tmp_path):
    # Distinguishes "the include/exclude entries appear somewhere" from
    # "the include entries are listed as modifiable and the exclude entries
    # as never-modifiable" -- catches the two lists being swapped in render.
    initialize_repo(tmp_path, CONFIG)
    text = (tmp_path / "AGENTS.md").read_text()
    _, _, scope_section = text.partition("## Scope")
    scope_section, _, _ = scope_section.partition("## Facts that cannot be inferred")
    modifiable, _, never_modifiable = scope_section.partition("Never modifiable:")
    for included in CONFIG.scope.include:
        assert included in modifiable, f"{included} missing from the modifiable list"
        assert included not in never_modifiable, f"{included} leaked into never-modifiable"
    for excluded in CONFIG.scope.exclude:
        assert excluded in never_modifiable, f"{excluded} missing from the never-modifiable list"
        assert excluded not in modifiable, f"{excluded} leaked into the modifiable list"


def test_agents_md_answers_the_six_required_sections(tmp_path):
    initialize_repo(tmp_path, CONFIG)
    text = (tmp_path / "AGENTS.md").read_text()
    for heading in (
        "## Purpose",
        "## Never allowed autonomously",
        "## Escalate",
        "## Scope",
        "## Facts that cannot be inferred",
        "## Where authority lives",
        "## Validation",
    ):
        assert heading in text, f"missing section: {heading}"


def test_agents_md_carries_no_generic_engineering_advice(tmp_path):
    # The directive: encode invariants, not intelligence. These phrases would all
    # be true of any competently-run repository and so are not invariants.
    initialize_repo(tmp_path, CONFIG)
    text = (tmp_path / "AGENTS.md").read_text().lower()
    for banned in (
        "write clean",
        "modular",
        "maintainable",
        "best practice",
        "remember to test",
        "inspect the codebase before",
    ):
        assert banned not in text, f"generic advice leaked into AGENTS.md: {banned}"


def test_agents_md_stays_within_the_persistent_budget(tmp_path):
    initialize_repo(tmp_path, CONFIG)
    size = (tmp_path / "AGENTS.md").stat().st_size
    assert size <= CONFIG.context.persistent_budget_bytes, (
        f"AGENTS.md is {size} bytes, over the configured persistent budget"
    )
