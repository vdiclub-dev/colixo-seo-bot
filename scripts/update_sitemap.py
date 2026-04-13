from __future__ import annotations

import argparse
from pathlib import Path

from common import load_settings, write_text


def url_from_path(path: Path, site_root: Path, base_url: str) -> str:
    relative = path.relative_to(site_root).as_posix()
    if relative == "index.html":
        return f"{base_url}/"
    if relative.endswith("/index.html"):
        return f"{base_url}/{relative[:-10]}/"
    return f"{base_url}/{relative}"


def build_sitemap(site_root: Path, base_url: str) -> str:
    html_files = sorted(site_root.rglob("*.html"))
    urls = [
        f"  <url><loc>{url_from_path(path, site_root, base_url)}</loc></url>"
        for path in html_files
        if ".git" not in path.parts
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls)
        + "\n</urlset>\n"
    )


def update_sitemap(site_root: Path) -> None:
    settings = load_settings()
    sitemap = build_sitemap(site_root, settings["site_base_url"].rstrip("/"))
    write_text(site_root / "sitemap.xml", sitemap)
    print(f"Updated sitemap: {site_root / 'sitemap.xml'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Update sitemap.xml for a static site directory.")
    parser.add_argument("--site-root", required=True, help="Path to the colixo-site working copy.")
    args = parser.parse_args()
    update_sitemap(Path(args.site_root).resolve())
