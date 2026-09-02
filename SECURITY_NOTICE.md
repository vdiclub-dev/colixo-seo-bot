# Security notice — required before activation

During the upgrade audit, the public SEO repository was found to contain literal credentials in its example environment file. Do not copy those values anywhere.

Required actions before enabling SEO Agent v2:

1. Revoke/rotate every credential that has appeared in that public file or Git history.
2. Replace the tracked `.env.example` with the sanitized template included here.
3. Store the replacement credentials only in repository secrets or another secret manager.
4. Review Git history / GitHub secret-scanning alerts; deleting a secret from the latest commit alone does not make an exposed secret safe.
5. Keep the Search Console credential read-only (`webmasters.readonly`).

The v2 workflow does not require a GitHub PAT for production publishing because direct production publishing is disabled.
