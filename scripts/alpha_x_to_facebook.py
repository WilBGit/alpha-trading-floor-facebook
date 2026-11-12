#!/usr/local/bin/python3
"""
Mirror confirmed Alpha Trading Floor X posts to the Alpha Trading Floor
Facebook Page as regular photo posts.

This is intentionally separate from the X publisher. It reads the X
publisher's successful-post ledger, preserves the verified X facts, adds a
short deterministic Facebook-native question and follow line, and uses a
persistent source-X-ID map to prevent duplicate Facebook writes. After exact
Facebook readback, it also persists the exact image in a local handoff manifest
that the YouTube release must consume for the same day.

Normal scheduled usage:
  python3 alpha_x_to_facebook.py

Explicit one-time mirror:
  python3 alpha_x_to_facebook.py --post-id <confirmed-x-post-id> --image-path <photo>

Scheduled Facebook poll:
  python3 alpha_x_to_facebook.py --poll

The live wrapper supplies X_TO_FACEBOOK_ENABLED=true. Direct invocation is
blocked unless that flag is present, and --dry-run never calls Facebook.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import UTC, datetime
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
from zoneinfo import ZoneInfo
from typing import Any


HOME = Path.home()
FB_PROJECT = Path(os.getenv("ALPHA_FACEBOOK_PROJECT_DIR", str(Path(__file__).resolve().parents[1] / "facebook_content"))).expanduser()
FB_SCRIPT = FB_PROJECT / "alpha_trading_floor.py"
X_STATE_FILE = HOME / ".hermes" / "state" / "alpha_trading_floor" / "poster_state.json"
MIRROR_STATE_FILE = HOME / ".hermes" / "state" / "alpha_trading_floor" / "x_facebook_mirror.json"
POLL_STATE_FILE = HOME / ".hermes" / "state" / "alpha_trading_floor" / "facebook_poll_state.json"
LOCK_FILE = HOME / ".hermes" / "state" / "alpha_trading_floor" / "x_facebook_mirror.lock"
IMAGE_HAMMING_DISTANCE_LIMIT = 4
FACEBOOK_FOLLOW_LINE = "Follow Alpha Trading Floor for the next market update."
FACEBOOK_QUESTIONS = (
    "What matters more here, the catalyst or the price level?",
    "Does this look like a one day move or a trend change?",
    "Which data point would confirm or challenge this move?",
)


def _youtube_cover_manifest_path() -> Path:
    configured = os.getenv("ALPHA_FACEBOOK_YOUTUBE_COVER_MANIFEST", "").strip()
    return Path(configured).expanduser() if configured else (
        HOME / ".hermes" / "state" / "alpha_trading_floor" / "facebook_youtube_cover.json"
    )


def _youtube_cover_dir() -> Path:
    configured = os.getenv("ALPHA_FACEBOOK_YOUTUBE_COVER_DIR", "").strip()
    return Path(configured).expanduser() if configured else (
        HOME / ".hermes" / "state" / "alpha_trading_floor" / "facebook_youtube_covers"
    )


def _safe_release_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "release"))[:80] or "release"


def persist_facebook_youtube_cover(
    image_path: str,
    post: dict[str, str],
    facebook: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist the exact verified Facebook image for the YouTube handoff."""
    source = Path(image_path).expanduser()
    if not source.is_file():
        raise MirrorError("verified Facebook image is missing before YouTube handoff")

    destination_dir = _youtube_cover_dir()
    destination_dir.mkdir(parents=True, exist_ok=True)
    release_date = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    suffix = source.suffix.lower() if source.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} else ".png"
    destination = destination_dir / (
        f"{release_date}_{_safe_release_component(post.get('x_post_id', 'release'))}{suffix}"
    )
    shutil.copy2(source, destination)
    image_sha256 = hashlib.sha256(destination.read_bytes()).hexdigest()
    record: dict[str, Any] = {
        "schema_version": 1,
        "status": "verified",
        "source": "facebook_verified_photo",
        "release_date": release_date,
        "x_post_id": str(post.get("x_post_id", "")),
        "facebook_post_id": (facebook or {}).get("post_id"),
        "facebook_permalink_url": (facebook or {}).get("permalink_url"),
        "youtube_cover_path": str(destination),
        "image_sha256": image_sha256,
        "updated_at": now_iso(),
    }
    atomic_json(_youtube_cover_manifest_path(), record)
    log(f"Persisted verified Facebook image for YouTube cover: {destination}")
    return record


def facebook_caption(x_text: str, x_post_id: str) -> str:
    """Make the X fact native to Facebook without changing its facts."""
    digest = hashlib.sha256(str(x_post_id).encode("utf-8")).digest()
    question = FACEBOOK_QUESTIONS[digest[0] % len(FACEBOOK_QUESTIONS)]
    return f"{x_text.strip()} {question} {FACEBOOK_FOLLOW_LINE}"


class MirrorError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def log(message: str, level: str = "INFO") -> None:
    print(f"[{datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {message}")


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception as exc:
        raise MirrorError(f"could not read JSON state {path}: {type(exc).__name__}") from exc


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)


def load_mirror_state() -> dict[str, Any]:
    state = read_json(MIRROR_STATE_FILE, None)
    if state is None:
        return {
            "schema_version": 2,
            "baseline_posted_at": None,
            "mirrors": {},
            "image_fingerprints": [],
        }
    if not isinstance(state, dict) or not isinstance(state.get("mirrors", {}), dict):
        raise MirrorError("mirror state has an invalid schema")
    if not isinstance(state.get("image_fingerprints", []), list):
        raise MirrorError("mirror state has an invalid image fingerprint ledger")
    state.setdefault("image_fingerprints", [])
    state["schema_version"] = max(2, int(state.get("schema_version", 1)))
    return state


def load_x_posts() -> list[dict[str, str]]:
    raw = read_json(X_STATE_FILE, None)
    if not isinstance(raw, dict) or not isinstance(raw.get("posts"), list):
        raise MirrorError("X publisher state is missing or invalid")

    posts: list[dict[str, str]] = []
    for item in raw["posts"]:
        if not isinstance(item, dict):
            continue
        result = item.get("result")
        if not isinstance(result, dict):
            continue
        post_id = str(result.get("id", "")).strip()
        text = str(item.get("text", "")).strip()
        posted_at = str(item.get("posted_at", "")).strip()
        if post_id and text:
            posts.append({"x_post_id": post_id, "text": text, "posted_at": posted_at})
    return posts


def image_fingerprint(module, path: str) -> dict[str, str]:
    """Fingerprint image bytes and normalized pixels for attachment uniqueness."""
    try:
        file_sha256 = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        with module.Image.open(path) as source:
            image = source.convert("RGB")
            resampling = getattr(module.Image, "Resampling", module.Image)
            resample = getattr(resampling, "LANCZOS", module.Image.LANCZOS)
            normalized = image.resize((64, 64), resample=resample)
            grayscale = image.convert("L").resize((16, 16), resample=resample)
            bits = "".join(
                "1" if grayscale.getpixel((x, y)) > grayscale.getpixel((x + 1, y)) else "0"
                for y in range(16)
                for x in range(15)
            )
            dhash = f"{int(bits, 2):0{(len(bits) + 3) // 4}x}"
            pixel_sha256 = hashlib.sha256(normalized.tobytes()).hexdigest()
        return {
            "file_sha256": file_sha256,
            "pixel_sha256": pixel_sha256,
            "dhash": dhash,
        }
    except Exception as exc:
        raise MirrorError(f"could not fingerprint photo asset: {type(exc).__name__}") from exc


def known_image_fingerprints(state: dict[str, Any]) -> list[dict[str, str]]:
    """Return the deduplicated image ledger, including per-mirror records."""
    candidates: list[Any] = list(state.get("image_fingerprints", []))
    candidates.extend(
        entry.get("image_fingerprint")
        for entry in state.get("mirrors", {}).values()
        if isinstance(entry, dict)
    )
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        fingerprint = {
            key: str(candidate.get(key, ""))
            for key in ("file_sha256", "pixel_sha256", "dhash")
        }
        if not any(fingerprint.values()):
            continue
        identity = tuple(fingerprint[key] for key in ("file_sha256", "pixel_sha256", "dhash"))
        if identity not in seen:
            seen.add(identity)
            result.append(fingerprint)
    return result


def dhash_distance(left: str, right: str) -> int | None:
    if not left or not right:
        return None
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError:
        return None


def assert_unique_image(module, path: str, state: dict[str, Any]) -> dict[str, str]:
    """Reject exact and near-identical images before any Facebook write."""
    candidate = image_fingerprint(module, path)
    for previous in known_image_fingerprints(state):
        if (
            candidate["file_sha256"] == previous.get("file_sha256")
            or candidate["pixel_sha256"] == previous.get("pixel_sha256")
        ):
            raise MirrorError("photo fingerprint already exists in Facebook mirror history")
        distance = dhash_distance(candidate["dhash"], previous.get("dhash", ""))
        if distance is not None and distance <= IMAGE_HAMMING_DISTANCE_LIMIT:
            raise MirrorError(
                f"photo is visually too similar to a prior Facebook attachment (dHash distance={distance})"
            )
    return candidate


def remember_image_fingerprint(state: dict[str, Any], fingerprint: dict[str, str]) -> None:
    ledger = state.setdefault("image_fingerprints", [])
    if not any(
        isinstance(item, dict)
        and item.get("pixel_sha256") == fingerprint.get("pixel_sha256")
        for item in ledger
    ):
        ledger.append(fingerprint)
    # Keep the ledger bounded without dropping recent protection.
    state["image_fingerprints"] = ledger[-500:]


def load_facebook_module():
    if not FB_SCRIPT.is_file():
        raise MirrorError(f"Facebook poster not found: {FB_SCRIPT}")

    # The sealed Facebook poster intentionally loads its project-local .env
    # relative to the current working directory. Match the live wrapper.
    os.chdir(FB_PROJECT)
    spec = importlib.util.spec_from_file_location("alpha_trading_floor_fb", FB_SCRIPT)
    if spec is None or spec.loader is None:
        raise MirrorError("could not load the Facebook poster module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def mirror_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+") as handle:
        os.chmod(LOCK_FILE, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MirrorError("another X-to-Facebook mirror is already running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def find_post(posts: list[dict[str, str]], state: dict[str, Any], requested_id: str | None) -> dict[str, str] | None:
    if requested_id:
        for post in reversed(posts):
            if post["x_post_id"] == requested_id:
                return post
        raise MirrorError(f"confirmed X post ID was not found in the X publisher ledger: {requested_id}")

    if not MIRROR_STATE_FILE.exists():
        raise MirrorError("mirror state is not initialized; use an explicit --post-id for the first mirror")

    baseline = state.get("baseline_posted_at")
    for post in posts:
        if post["x_post_id"] in state["mirrors"]:
            continue
        if baseline and post.get("posted_at"):
            try:
                posted_at = datetime.fromisoformat(post["posted_at"])
                baseline_at = datetime.fromisoformat(str(baseline))
                if posted_at <= baseline_at:
                    continue
            except ValueError:
                # An unparseable timestamp is not safe to classify as new.
                continue
        return post
    return None


def exact_live_facebook_post(module, text: str) -> dict[str, Any] | None:
    """Read the public feed once; never write or retry here."""
    for edge in ("published_posts", "feed"):
        try:
            response = module.requests.get(
                f"https://graph.facebook.com/{module.FACEBOOK_PAGE_ID}/{edge}",
                params={
                    "fields": "id,message,created_time,permalink_url,full_picture",
                    "limit": "100",
                    "access_token": module.FACEBOOK_PAGE_ACCESS_TOKEN,
                },
                timeout=module.FACEBOOK_TIMEOUT_SECONDS,
            )
            if not 200 <= response.status_code < 300:
                continue
            data = response.json()
            if isinstance(data, dict) and data.get("error"):
                continue
            for item in data.get("data", []):
                if item.get("message", "").strip() != text.strip():
                    continue
                if not item.get("full_picture"):
                    continue
                return {
                    "edge": edge,
                    "post_id": item.get("id"),
                    "created_time": item.get("created_time"),
                    "permalink_url": item.get("permalink_url"),
                    "message": item.get("message", ""),
                }
        except Exception:
            continue
    return None


def exact_live_facebook_text_post(module, text: str) -> dict[str, Any] | None:
    """Read back an exact text-only Facebook post without requiring media."""
    for edge in ("published_posts", "feed"):
        try:
            response = module.requests.get(
                f"https://graph.facebook.com/{module.FACEBOOK_PAGE_ID}/{edge}",
                params={
                    "fields": "id,message,created_time,permalink_url",
                    "limit": "100",
                    "access_token": module.FACEBOOK_PAGE_ACCESS_TOKEN,
                },
                timeout=module.FACEBOOK_TIMEOUT_SECONDS,
            )
            if not 200 <= response.status_code < 300:
                continue
            data = response.json()
            if isinstance(data, dict) and data.get("error"):
                continue
            for item in data.get("data", []):
                if item.get("message", "").strip() == text.strip():
                    return {
                        "edge": edge,
                        "post_id": item.get("id"),
                        "created_time": item.get("created_time"),
                        "permalink_url": item.get("permalink_url"),
                        "message": item.get("message", ""),
                    }
        except Exception:
            continue
    return None


def build_poll_text(module, poll: dict[str, Any]) -> str:
    question = str(poll.get("question", "")).strip()
    options = [str(option).strip() for option in poll.get("options", []) if str(option).strip()]
    if not question or len(options) < 2:
        raise MirrorError("poll needs a question and at least two options")
    text = f"{question}\n\n"
    text += "".join(f"{index}. {option}\n" for index, option in enumerate(options, 1))
    text += "\nVote with your number or drop your answer below."
    text += str(getattr(module, "POST_CTA_FOOTER", ""))
    return text


def load_poll_state() -> dict[str, Any]:
    state = read_json(POLL_STATE_FILE, {})
    if not isinstance(state, dict):
        raise MirrorError("Facebook poll state has an invalid schema")
    return state


def run_poll(args: argparse.Namespace) -> int:
    """Run one idempotent, read-back-verified Tuesday Facebook poll."""
    if args.dry_run:
        os.environ["DRY_RUN"] = "true"

    module = load_facebook_module()
    now_pt = datetime.now(ZoneInfo("America/Los_Angeles"))
    iso = now_pt.isocalendar()
    poll_questions = getattr(module, "POLL_QUESTIONS", [])
    if not isinstance(poll_questions, list) or not poll_questions:
        raise MirrorError("Facebook poll question bank is missing")
    poll_index = (int(iso.week) - 1) % len(poll_questions)
    poll = poll_questions[poll_index]
    poll_key = f"{iso.year}-W{int(iso.week):02d}-{poll_index}"
    poll_text = build_poll_text(module, poll)
    state = load_poll_state()

    if state.get("poll_key") == poll_key:
        if state.get("status") in {"verified", "verified_existing"}:
            log(f"SKIP: Facebook poll already verified for {poll_key}")
            return 0
        log("STOP: this week's Facebook poll has a prior non-verified result; refusing an automatic retry", "ERROR")
        return 2

    if args.dry_run or module.DRY_RUN:
        log(f"DRY RUN: Facebook poll {poll_key} passes validation; no Facebook read or write")
        return 0

    if not truthy(os.getenv("X_TO_FACEBOOK_ENABLED")):
        log("BLOCKED: X_TO_FACEBOOK_ENABLED is not true.", "ERROR")
        return 2
    if not module.verify_facebook_page_access():
        atomic_json(POLL_STATE_FILE, {
            "poll_key": poll_key,
            "status": "blocked_before_write",
            "updated_at": now_iso(),
        })
        log("BLOCKED: Facebook Page identity check failed; no poll write attempted.", "ERROR")
        return 2

    existing = exact_live_facebook_text_post(module, poll_text)
    if existing:
        atomic_json(POLL_STATE_FILE, {
            "poll_key": poll_key,
            "status": "verified_existing",
            "facebook": existing,
            "updated_at": now_iso(),
        })
        log(f"SKIP: exact Facebook poll already exists; verified {existing.get('permalink_url')}")
        return 0

    if not module.post_poll_to_facebook(poll.get("question"), poll.get("options", [])):
        atomic_json(POLL_STATE_FILE, {
            "poll_key": poll_key,
            "status": "write_failed",
            "updated_at": now_iso(),
        })
        log("BLOCKED: Facebook poll write failed; no retry attempted.", "ERROR")
        return 2

    verified = exact_live_facebook_text_post(module, poll_text)
    if not verified:
        atomic_json(POLL_STATE_FILE, {
            "poll_key": poll_key,
            "status": "accepted_readback_ambiguous",
            "updated_at": now_iso(),
        })
        log("STOP: Facebook accepted the poll write but exact readback was ambiguous; no retry attempted.", "ERROR")
        return 2

    atomic_json(POLL_STATE_FILE, {
        "poll_key": poll_key,
        "status": "verified",
        "facebook": verified,
        "updated_at": now_iso(),
    })
    log(f"POLL POSTED and verified: {verified.get('permalink_url')}")
    return 0


def persist_mirror(
    state: dict[str, Any],
    post: dict[str, str],
    *,
    status: str,
    facebook: dict[str, Any] | None = None,
    facebook_text: str | None = None,
    reason: str | None = None,
    attempted_at: str | None = None,
    image_fingerprint: dict[str, str] | None = None,
    release_asset: dict[str, Any] | None = None,
) -> None:
    entry: dict[str, Any] = {
        "x_post_id": post["x_post_id"],
        "x_content_sha256": hashlib.sha256(post["text"].encode("utf-8")).hexdigest(),
        "status": status,
        "updated_at": now_iso(),
    }
    if facebook_text:
        entry["facebook_caption_sha256"] = hashlib.sha256(
            facebook_text.encode("utf-8")
        ).hexdigest()
        entry["facebook_caption_mode"] = (
            "native_question_follow" if facebook_text != post["text"] else "legacy_exact_x"
        )
    if attempted_at:
        entry["attempted_at"] = attempted_at
    if reason:
        entry["reason"] = reason
    if image_fingerprint:
        entry["image_fingerprint"] = image_fingerprint
        remember_image_fingerprint(state, image_fingerprint)
    if release_asset:
        entry["facebook_youtube_cover"] = release_asset
    if facebook:
        entry["facebook_edge"] = facebook.get("edge")
        entry["facebook_post_id"] = facebook.get("post_id")
        entry["facebook_created_time"] = facebook.get("created_time")
        entry["facebook_permalink_url"] = facebook.get("permalink_url")
    state["mirrors"][post["x_post_id"]] = entry
    if not state.get("baseline_posted_at"):
        state["baseline_posted_at"] = now_iso()
    state["schema_version"] = 2
    atomic_json(MIRROR_STATE_FILE, state)


def record_in_facebook_history(module, text: str) -> None:
    """Keep the legacy generator's dedup history aligned, if writable."""
    try:
        history = module.load_posted_quotes()
        normalized = module.normalize_fact(text)
        if not any(module.normalize_fact(str(item)) == normalized for item in history):
            module.save_posted_quotes(history + [text])
        module.record_post_time()
    except Exception as exc:
        # The public post is already verified. This is local bookkeeping only.
        log(f"Facebook history bookkeeping warning: {type(exc).__name__}", "WARN")


def choose_image(
    module,
    text: str,
    supplied: str | None,
    state: dict[str, Any],
) -> tuple[str, dict[str, str]]:
    def accept(path: str) -> tuple[str, dict[str, str]]:
        resolved = str(Path(path).resolve())
        return resolved, assert_unique_image(module, resolved, state)

    if supplied:
        image = str(Path(supplied).expanduser().resolve())
        if not module.validate_photo_asset(image):
            raise MirrorError("supplied image failed the Facebook photo validator")
        return accept(image)

    # The mirror is a scheduled distribution path, not an image-generation job.
    # Use a verified unused local photo first. If it is exhausted or visually
    # repeated, continue to the bounded providers; never recycle an old photo.
    try:
        local = module.get_local_background()
        if local and module.validate_photo_asset(local):
            final_path = module.create_final_image(local, text)
            if final_path and module.validate_photo_asset(final_path):
                try:
                    selected = accept(final_path)
                    log(f"Using verified unique local Facebook photo: {final_path}")
                    return selected
                except MirrorError as exc:
                    log(f"Local Facebook photo rejected for uniqueness: {exc}", "WARN")
    except Exception as exc:
        log(f"Local Facebook photo fallback unavailable: {type(exc).__name__}", "WARN")

    prompt = (
        "Create one clean photorealistic 16:9 editorial finance image for this market caption. "
        "No readable text, logos, screenshots, faces, watermarks, UI, or chart labels. "
        "Show a restrained professional market atmosphere that matches the topic, without "
        "reproducing the caption inside the image. Caption topic: "
        + text[:500]
    )
    image = module.generate_background_asset(prompt, text)
    if image and module.validate_photo_asset(image):
        return accept(image)
    raise MirrorError("no verified unique photo asset available; refusing text-only or repeated publication")


def add_common_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--post-id", help="explicit confirmed X post ID for a one-time mirror")
    parser.add_argument("--image-path", help="verified local photo to use instead of generating one")
    parser.add_argument("--poll", action="store_true", help="publish the weekly Facebook engagement poll")
    parser.add_argument("--dry-run", action="store_true", help="validate and preview without Facebook reads or writes")


def run(args: argparse.Namespace) -> int:
    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
    if args.poll:
        return run_poll(args)

    module = load_facebook_module()
    posts = load_x_posts()
    state = load_mirror_state()
    post = find_post(posts, state, args.post_id)
    if post is None:
        # no_agent cron jobs deliver stdout verbatim; remain silent when the
        # daily X writer had no verified post or nothing new is pending.
        return 0

    x_text = post["text"]
    facebook_text = facebook_caption(x_text, post["x_post_id"])
    if len(x_text) < 40 or len(x_text) > 400 or not module.is_valid_fact(x_text):
        raise MirrorError("exact X caption failed the Facebook caption validator")
    if len(facebook_text) > 600 or not module.is_valid_fact(facebook_text):
        raise MirrorError("Facebook-native caption failed the caption validator")
    if module.POST_AS_REEL or not module.REQUIRE_PHOTO_POST:
        raise MirrorError("photo-only Facebook contract is not active")

    if args.dry_run or module.DRY_RUN:
        log(
            f"DRY RUN: X post {post['x_post_id']} passes caption and photo-only gates; "
            f"Facebook-native caption={len(facebook_text)} chars; no Facebook read or write."
        )
        return 0

    if not truthy(os.getenv("X_TO_FACEBOOK_ENABLED")):
        log("BLOCKED: X_TO_FACEBOOK_ENABLED is not true.", "ERROR")
        return 2

    # An explicit replay of an already-recorded mirror must verify the live
    # Facebook object before doing any media work. Never spend generation
    # time, or risk a duplicate write, for a known-complete source ID.
    prior = state["mirrors"].get(post["x_post_id"])
    if prior and prior.get("status") in {"verified", "verified_existing"}:
        existing = None
        matched_caption = facebook_text
        for candidate in (facebook_text, x_text):
            existing = exact_live_facebook_post(module, candidate)
            if existing:
                matched_caption = existing.get("message") or candidate
                break
        if existing:
            persist_mirror(
                state,
                post,
                status="verified_existing",
                facebook=existing,
                facebook_text=matched_caption,
            )
            record_in_facebook_history(module, matched_caption)
            log(f"SKIP: X post already mirrored; exact Facebook readback verified {existing.get('permalink_url')}")
            return 0
        raise MirrorError("mirror state says this X post was verified, but exact Facebook readback is absent; refusing duplicate")

    image, image_fingerprint_value = choose_image(module, facebook_text, args.image_path, state)
    log(
        f"Candidate X post {post['x_post_id']} validated ({len(x_text)} X chars, "
        f"{len(facebook_text)} Facebook chars); photo={image}"
    )

    existing = None
    matched_caption = facebook_text
    for candidate in (facebook_text, x_text):
        existing = exact_live_facebook_post(module, candidate)
        if existing:
            matched_caption = existing.get("message") or candidate
            break
    if existing:
        persist_mirror(
            state,
            post,
            status="verified_existing",
            facebook=existing,
            facebook_text=matched_caption,
        )
        record_in_facebook_history(module, matched_caption)
        log(f"SKIP: exact caption already exists on Facebook; verified {existing.get('permalink_url')}")
        return 0

    if not module.verify_facebook_page_access():
        persist_mirror(
            state,
            post,
            status="blocked_before_write",
            facebook_text=facebook_text,
            reason="facebook_page_access_check_failed",
            image_fingerprint=image_fingerprint_value,
        )
        log("BLOCKED: Facebook Page identity check failed; no write attempted.", "ERROR")
        return 2

    attempted_at = now_iso()
    if not module.post_to_facebook(facebook_text, image):
        persist_mirror(
            state,
            post,
            status="write_failed_no_verified_result",
            facebook_text=facebook_text,
            reason="facebook_photo_write_returned_false",
            attempted_at=attempted_at,
            image_fingerprint=image_fingerprint_value,
        )
        log("BLOCKED: Facebook photo write did not return success; no retry attempted.", "ERROR")
        return 2

    # post_to_facebook performs its own bounded readback. This second exact
    # read gives this mirror state its concrete permalink/ID and is the final
    # claim boundary for the scheduled workflow.
    verified = module.verify_facebook_publication(facebook_text)
    if not verified.get("verified"):
        persist_mirror(
            state,
            post,
            status="accepted_readback_ambiguous",
            facebook_text=facebook_text,
            reason="facebook_write_returned_success_but_exact_readback_failed",
            attempted_at=attempted_at,
            image_fingerprint=image_fingerprint_value,
        )
        log("STOP: Facebook accepted the write but exact readback is ambiguous; no retry attempted.", "ERROR")
        return 2

    try:
        release_asset = persist_facebook_youtube_cover(image, post, verified)
    except Exception as exc:
        # The Facebook post is already verified. Preserve that result while
        # making the missing YouTube handoff explicit for the next pipeline run.
        release_asset = None
        log(f"YouTube cover handoff unavailable: {type(exc).__name__}", "WARN")

    persist_mirror(
        state,
        post,
        status="verified",
        facebook=verified,
        facebook_text=facebook_text,
        attempted_at=attempted_at,
        image_fingerprint=image_fingerprint_value,
        release_asset=release_asset,
    )
    record_in_facebook_history(module, facebook_text)
    log(f"POSTED and verified: {verified.get('permalink_url')} (Facebook ID {verified.get('post_id')})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Mirror confirmed Alpha Trading Floor X posts to Facebook")
    add_common_parser(parser)
    args = parser.parse_args()
    try:
        with mirror_lock():
            return run(args)
    except MirrorError as exc:
        log(str(exc), "ERROR")
        return 2
    except KeyboardInterrupt:
        log("Interrupted before completion.", "ERROR")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
