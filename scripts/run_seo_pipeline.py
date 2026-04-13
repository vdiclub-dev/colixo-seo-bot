from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


def run_step(script_name: str) -> None:
    script_path = ROOT_DIR / "scripts" / script_name
    print(f"Running step: {script_name}")
    subprocess.run([sys.executable, str(script_path)], cwd=ROOT_DIR, check=True)


def main() -> None:
    run_step("generate_pages.py")
    run_step("generate_articles.py")
    run_step("push_to_site_repo.py")
    print("SEO pipeline completed.")


if __name__ == "__main__":
    main()
