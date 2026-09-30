import subprocess
from pathlib import Path

from dev_orchestration import release

REPO = Path(__file__).resolve().parents[1]


def test_promote_builds_self_contained_release(tmp_path):
    releases, bin_dir = tmp_path / "releases", tmp_path / "bin"

    commit = release.promote(REPO, releases, bin_dir)

    venv = releases / commit / "venv"
    where = subprocess.run(
        [venv / "bin" / "python", "-c", "import dev_orchestration as d; print(d.__file__)"],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
    ).stdout.strip()
    assert where.startswith(str(releases / commit))
    assert (Path(where).parent / "roles" / "planner.md").is_file()
    version = subprocess.run(
        [bin_dir / "dev-orch", "--version"], capture_output=True, text=True, check=True
    )
    assert version.stdout.strip() == commit
