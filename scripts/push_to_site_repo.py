from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from common import (
    GENERATED_DIR,
    TMP_DIR,
    bootstrap_env,
    copy_tree_contents,
    ensure_directory,
    get_required_env,
    load_settings,
    optional_env,
    run_command,
)
from update_sitemap import update_sitemap


def authenticated_repo_url(repo_url: str, token: str) -> str:
    if repo_url.startswith("https://") and token:
        return repo_url.replace("https://", f"https://x-access-token:{token}@")
    return repo_url


def prepare_site_repo(settings: dict) -> Path:
    repo_url = optional_env("SITE_REPO_URL", settings["site_repo_url"])
    repo_branch = settings.get("site_repo_branch", "main")
    repo_dir = TMP_DIR / settings.get("site_repo_local_dir", "colixo-site")
    token = get_required_env("SITE_REPO_PAT")
    push_url = authenticated_repo_url(repo_url, token)

    ensure_directory(TMP_DIR)
    if repo_dir.exists():
        shutil.rmtree(repo_dir)

    run_command(["git", "clone", "--branch", repo_branch, push_url, str(repo_dir)])
    # Keep a clean fetch URL while preserving authenticated push access for CI runners.
    run_command(["git", "remote", "set-url", "origin", repo_url], cwd=repo_dir)
    run_command(["git", "remote", "set-url", "--push", "origin", push_url], cwd=repo_dir)
    return repo_dir


def copy_generated_files(site_root: Path) -> None:
    print(f"Copying generated pages into: {site_root}")
    copy_tree_contents(GENERATED_DIR / "pages", site_root)
    blog_dir = site_root / "blog"
    ensure_directory(blog_dir)
    copy_tree_contents(GENERATED_DIR / "blog", blog_dir)


def commit_and_push(site_root: Path, settings: dict) -> None:
    author_name = optional_env("GIT_AUTHOR_NAME", settings.get("git_author_name", "Colixo SEO Bot"))
    author_email = optional_env("GIT_AUTHOR_EMAIL", settings.get("git_author_email", "seo-bot@colixo.ch"))

    run_command(["git", "config", "user.name", author_name], cwd=site_root)
    run_command(["git", "config", "user.email", author_email], cwd=site_root)
    run_command(["git", "add", "."], cwd=site_root)

    status = run_command(["git", "status", "--porcelain"], cwd=site_root)
    if not status:
        print("No changes detected in colixo-site.")
        return

    commit_message = settings.get("commit_message", "chore: refresh SEO pages and sitemap")
    run_command(["git", "commit", "-m", commit_message], cwd=site_root)
    run_command(["git", "push", "origin", settings.get("site_repo_branch", "main")], cwd=site_root)
    print("Committed and pushed changes to colixo-site.")


def push_to_site_repo() -> None:
    bootstrap_env()
    settings = load_settings()
    site_root = prepare_site_repo(settings)
    copy_generated_files(site_root)
    update_sitemap(site_root)
    commit_and_push(site_root, settings)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clone colixo-site, copy generated files, and push changes.")
    parser.parse_args()
    push_to_site_repo()
