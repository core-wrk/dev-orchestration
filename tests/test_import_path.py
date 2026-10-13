from pathlib import Path

import dev_orchestration.git.repo as repo_module


def test_package_imports_from_this_checkout():
    source_root = Path(__file__).resolve().parents[1] / "src"
    assert Path(repo_module.__file__).resolve().is_relative_to(source_root)
