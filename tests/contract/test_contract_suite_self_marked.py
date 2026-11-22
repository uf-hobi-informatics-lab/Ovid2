"""Contract: ``-m contract`` selects every test in ``tests/contract/``."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_every_contract_file_is_selected_by_the_marker(repo_root: Path) -> None:
    """Collected-with-marker == collected-without-marker for this directory."""

    def _collect(*extra: str) -> set[str]:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-p",
                "no:cacheprovider",
                *extra,
                "tests/contract",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        return {line for line in proc.stdout.splitlines() if "::" in line}

    everything = _collect()
    marked = _collect("-m", "contract")
    assert everything, "no contract tests collected"
    missing = sorted(everything - marked)
    assert not missing, "contract tests not selected by -m contract:\n" + "\n".join(missing[:20])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
