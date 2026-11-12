# Alpha Trading Floor Facebook Auto Content Publisher

The guarded Facebook content path for Alpha Trading Floor. It generates complete finance posts, validates the caption and visual, verifies the configured Page identity, publishes a regular photo post through the Meta Graph `/photos` edge, and reads the publication back before recording success.

## Included components

- `facebook_content/alpha_trading_floor.py`: sealed caption, image, validation, cooldown, and Page publication flow.
- `facebook_content/alpha_xai_image.py`: optional native Hermes image-provider helper.
- `scripts/alpha_x_to_facebook.py`: mirrors a confirmed X post without rewriting its verified facts and prevents duplicate source-X publication.
- `scripts/alpha_fb_poster_runner.py`: bounded process-group runner.
- `scripts/alpha_fb_poster.sh`: one-shot scheduled poster.
- `scripts/alpha_facebook_poll.sh`: bounded weekly poll path.
- `scripts/alpha_x_to_facebook.sh`: bounded X-to-Facebook mirror wrapper.

## Publishing policy

This project creates regular Facebook photo feed posts only. It never publishes Reels and never falls back to text-only publication. A clean, verified photo is required. Captions must be complete, fact-checked, and include a clear market, options, or business-impact opinion when the post is breaking news.

The checked-in publication copy defaults to `DRY_RUN=true`, `POST_VERIFY_ENABLED=true`, and no configured Page credentials. It does not publish anything during validation. Set `DRY_RUN=false` only in a separately approved deployment with credentials injected outside Git and with a confirmed Page target.

## Local setup

Use a virtual environment and install `requirements.txt`. Set `ALPHA_FACEBOOK_PROJECT_DIR` when the content implementation lives outside this repository. Provide `FACEBOOK_PAGE_ID` and `FACEBOOK_PAGE_ACCESS_TOKEN` through a secret manager or an ignored local environment file. Never paste either value, a Facebook password, a one-time code, or an app secret into chat or source control.

The X-to-Facebook mirror requires `X_TO_FACEBOOK_ENABLED=true` in addition to its explicit command. It reads confirmed X state and stores mirror state under `~/.hermes/state/alpha_trading_floor`; those files are intentionally excluded.

## Operational guarantees

- Page auth is checked before every live write.
- The Page ID returned by Meta must match the configured Page ID.
- Publication uses `/photos`, not `/feed` for the regular photo path.
- A successful write is not accepted until the exact post is read back or the run fails closed without retrying blindly.
- Cooldowns, deduplication, image reuse checks, and process timeouts prevent accidental repeats.

No live Facebook post is performed by repository validation.
