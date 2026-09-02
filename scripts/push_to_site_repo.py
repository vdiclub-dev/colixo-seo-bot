"""Legacy production push is intentionally disabled.

SEO Agent v2 must never publish directly to the Colixo production site.
Future content changes are prepared on a branch and reviewed through a PR.
"""


def main() -> None:
    raise SystemExit(
        "Direct SEO push disabled by Colixo SEO Agent v2. "
        "Use a reviewed branch/PR workflow instead."
    )


if __name__ == "__main__":
    main()
