import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TEMPLATE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEMPLATE_ROOT / "scripts"))

from pbi_promote.config import load_config  # noqa: E402


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


@pytest.fixture
def template_root() -> Path:
    return TEMPLATE_ROOT


@pytest.fixture
def config():
    return load_config(TEMPLATE_ROOT / "config")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository seeded with the template's config, CODEOWNERS and sample units."""
    root = tmp_path / "repo"
    root.mkdir()
    for name in ("config", "src"):
        shutil.copytree(TEMPLATE_ROOT / name, root / name)
    (root / ".github").mkdir()
    shutil.copy2(TEMPLATE_ROOT / ".github" / "CODEOWNERS", root / ".github" / "CODEOWNERS")
    (root / "README.md").write_text("test repo\n", encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "config", "core.autocrlf", "false")
    git(root, "config", "core.longpaths", "true")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture
def commit():
    def _commit(repo: Path, message: str = "change") -> str:
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", message)
        return git(repo, "rev-parse", "HEAD")

    return _commit


@pytest.fixture
def head():
    def _head(repo: Path) -> str:
        return git(repo, "rev-parse", "HEAD")

    return _head
