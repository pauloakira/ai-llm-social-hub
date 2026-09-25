import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_gitignore_does_not_exclude_bundled_files():
    """The wheel build honors .gitignore, so an ignored package file silently vanishes from releases."""
    files = [str(p.relative_to(ROOT)) for p in (ROOT / "src" / "llm_hub").rglob("*") if p.is_file()
             and "__pycache__" not in p.parts]
    assert any("skills" in f for f in files)
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"], input="\n".join(files), capture_output=True, text=True,
        cwd=ROOT,
    ).stdout.split()
    assert ignored == []
