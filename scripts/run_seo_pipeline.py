"""Safe entrypoint for Colixo SEO Agent v2.

The legacy pipeline generated pages and pushed directly to a site repository.
V2 is intentionally read-only with respect to production: monitor, analyse, propose.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "seo_agent_v2.py")],
        cwd=ROOT,
        check=True,
    )


if __name__ == "__main__":
    main()
