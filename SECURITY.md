# Security

## Secrets

Never commit `FACEBOOK_PAGE_ACCESS_TOKEN`, `FACEBOOK_PAGE_ID` values from private configuration, OAuth files, browser profiles, app secrets, passwords, one-time codes, or `.env` files. Inject secrets at runtime through a secret manager or owner-only environment file.

The repository excludes generated images, post history, logs, cooldown markers, local state, and retired code. Provider errors must be reduced to safe status categories; do not log Graph response bodies or authorization headers.

## Fail-closed publication

The checked-in defaults are dry-run and regular photo posts only. Live publication requires an explicit `DRY_RUN=false`, a verified Page identity, a valid token with the minimum required permissions, a complete caption, a clean visual, and successful readback. If generation, authentication, validation, or readback fails, the job must stop without a retry that could duplicate content.

No Reels path is supported for this project. No text-only fallback is permitted.

## Review checklist

Before enabling a deployment, review the exact caption, image, Page ID, publication mode, cooldown state, and public readback behavior. Run with a non-production Page first. Rotate and revoke credentials immediately if they appear in a log, issue, pull request, terminal capture, or chat message.
