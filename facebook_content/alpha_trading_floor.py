"""
Alpha Trading Floor — Facebook Auto-Poster
============================================
Posts high-value financial content to the Alpha Trading Floor Facebook page.

SCHEDULED MODE: Hermes launches one bounded TEST_MODE cycle at 09:00, 11:00,
and 13:00 PT on weekdays. WATCHDOG MODE is optional and polls every 30 minutes
for breaking news when the script is run outside the one-shot wrapper. The
shared two-hour cooldown prevents duplicate posts.

Posts include an opinion on how the content impacts financial markets,
options, and businesses — like a trader giving their read.

Content pillars (NOT random — all must be real, specific, and impactful):
  - Breaking news: Trump policy, SEC, Fed, oil, war, major layoffs —
    always includes impact opinion on markets/options/businesses
  - Policy & regulation: government actions that move markets
  - Geopolitics & commodities: oil, war, sanctions, supply chains
  - Big business moves: layoffs, M&A, earnings shocks
  - Wealth building: specific, actionable financial insights
  - Market mechanics: how markets work, smart money behavior
  - Financial facts: verified, surprising money knowledge

Voice: experienced trader talking to another trader.
  NOT: CNBC, corporate analyst, generic AI, crypto spam, financial advisor
  IS:  calm, direct, slightly aggressive when appropriate, natural, human

Model path:
  Caption chain: GPT-5.5 -> Grok 4.6 through Hermes CLI.
  Image chain: GPT Image 2 through OpenRouter -> native xAI Grok Imagine -> verified local photo.
  No direct LLM HTTP API calls are used.

Usage:
  python alpha_trading_floor.py              # run watchdog (polls every 30min)
  TEST_MODE=true python alpha_trading_floor.py   # single test post
  DRY_RUN=true python alpha_trading_floor.py     # dry run (no publish)
"""

import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import uuid
import time
from datetime import datetime, UTC
from difflib import SequenceMatcher
from pathlib import Path

import requests
import schedule
from PIL import Image

# --- ENV LOADER ---
def load_env_file(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v

load_env_file()

# --- SEALED FLOW CONFIG ---
# This is the only supported generation and publication flow. Keep these
# invariants hard-coded so stale .env values cannot switch the runtime back to
# an older model, API path, or Reel/text-only format.
FLOW_ID = "hermes-cli-gpt-image2-xai-image-facebook-photo-v3"
CODEX_MODEL = "gpt-5.5"
HERMES_CLI = "hermes"
CODEX_PROVIDER = "openai-codex"
GROK_TEXT_PROVIDER = "xai-oauth"
GROK_TEXT_MODEL = "grok-4.6"
# Keep every provider attempt bounded. The wrapper adds a process-group deadline.
CODEX_TIMEOUT_SECONDS = max(15, min(int(os.getenv("CODEX_TIMEOUT_SECONDS", "45")), 75))
CODEX_RUN_BUDGET_SECONDS = max(10, min(int(os.getenv("CODEX_RUN_BUDGET_SECONDS", "35")), 60))
FALLBACK_TIMEOUT_SECONDS = max(15, min(int(os.getenv("FALLBACK_TIMEOUT_SECONDS", "35")), 60))
FALLBACK_RUN_BUDGET_SECONDS = max(10, min(int(os.getenv("FALLBACK_RUN_BUDGET_SECONDS", "30")), 50))
GPT_IMAGE_TIMEOUT_SECONDS = max(30, min(int(os.getenv("GPT_IMAGE_TIMEOUT_SECONDS", "90")), 150))
GPT_IMAGE_RUN_BUDGET_SECONDS = max(20, min(int(os.getenv("GPT_IMAGE_RUN_BUDGET_SECONDS", "60")), 100))

FACEBOOK_PAGE_ACCESS_TOKEN = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "")
FACEBOOK_PAGE_ID = os.getenv("FACEBOOK_PAGE_ID", "")
POST_HISTORY_FILE = os.getenv("POST_HISTORY_FILE", "posted_quotes.json")
LOG_FILE = os.getenv("LOG_FILE", "alpha_trading_floor.log")
PROMPT_FILE = os.getenv("PROMPT_FILE", "prompts.json")
IMAGE_PROMPT_LOG = os.getenv("IMAGE_PROMPT_LOG", "last_image_prompt.txt")

MAX_HISTORY = int(os.getenv("POST_HISTORY_MAX", 500))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
FB_DEBUG = os.getenv("FB_DEBUG", "false").lower() == "true"
LLM_DEBUG = os.getenv("LLM_DEBUG", "false").lower() == "true"
TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"
GROK_ENABLED = os.getenv("GROK_ENABLED", "true").lower() == "true"
GROK_TIMEOUT_SECONDS = int(os.getenv("GROK_TIMEOUT_SECONDS", "60"))
GROK_IMAGE_DIR = os.getenv("GROK_IMAGE_DIR", "grok_generated")
GPT_IMAGE_DIR = os.getenv("GPT_IMAGE_DIR", "gpt_generated")
XAI_IMAGE_MODEL = os.getenv("XAI_IMAGE_MODEL", "grok-imagine-image-quality")
XAI_IMAGE_HELPER = Path(__file__).with_name("alpha_xai_image.py")
BACKGROUND_ROTATION_FILE = os.getenv("BACKGROUND_ROTATION_FILE", os.path.join("backgrounds", ".rotation_state.json"))
GPT_IMAGE_CIRCUIT_FILE = os.getenv("GPT_IMAGE_CIRCUIT_FILE", str(Path(__file__).with_name("gpt_image_circuit.json")))
GPT_IMAGE_CIRCUIT_THRESHOLD = int(os.getenv("GPT_IMAGE_CIRCUIT_THRESHOLD", "3"))
GPT_IMAGE_CIRCUIT_COOLDOWN_HOURS = int(os.getenv("GPT_IMAGE_CIRCUIT_COOLDOWN_HOURS", "24"))
IMAGE_DOWNLOAD_TIMEOUT_SECONDS = max(10, min(int(os.getenv("IMAGE_DOWNLOAD_TIMEOUT_SECONDS", "30")), 60))
# Hard-disabled permanently for this page. The active flow is a regular photo
# post; stale POST_AS_REEL values in .env are intentionally ignored.
POST_AS_REEL = False
REQUIRE_PHOTO_POST = True
FACEBOOK_TIMEOUT_SECONDS = int(os.getenv("FACEBOOK_TIMEOUT_SECONDS", "45"))
POST_VERIFY_ENABLED = os.getenv("POST_VERIFY_ENABLED", "true").lower() == "true"
try:
    POST_VERIFY_TIMEOUT_SECONDS = max(
        10, min(int(os.getenv("POST_VERIFY_TIMEOUT_SECONDS", "30")), 45)
    )
except (TypeError, ValueError):
    POST_VERIFY_TIMEOUT_SECONDS = 30

# Watchdog: poll interval (minutes) and cooldown between posts (hours)
POLL_INTERVAL_MIN = int(os.getenv("POLL_INTERVAL_MIN", "30"))
MIN_HOURS_BETWEEN_POSTS = float(os.getenv("MIN_HOURS_BETWEEN_POSTS", "2"))
# Track last post time to enforce cooldown
LAST_POST_FILE = os.getenv("LAST_POST_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".last_post_time"))

# Daily scheduled posts — same times as X (market hours, weekdays)
DAILY_POST_TIMES = [t.strip() for t in os.getenv("DAILY_POST_TIMES", "09:00,11:00,13:00").split(",") if t.strip()]
# Whether to also run the watchdog for breaking news between scheduled posts
WATCHDOG_ENABLED = os.getenv("WATCHDOG_ENABLED", "true").lower() == "true"

# Dedup: reject posts above this similarity to any recent post
SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.75"))

# --- CONTENT PILLARS ---
CONTENT_PILLARS = [
    "policy_regulation",       # Trump admin, SEC, Fed, tariffs, trade policy
    "geopolitics_commodities", # Oil, war, sanctions, OPEC, supply chain
    "big_business_moves",      # Layoffs, M&A, earnings shocks, CEO moves
    "wealth_building",         # Specific money decisions, strategies, mechanisms
    "market_mechanics",        # How markets work, why things move, smart money behavior
    "options_insight",         # Options strategies, Greeks, flow, IV — trader-level content
    "breaking_news",           # Breaking financial news with impact opinion
]

# ─── Engagement Hooks (one appended to every post) ────────────────────────
# Each hook is a specific, answerable question tied to a pillar category.
# FB algorithm weights comments 6x more than likes. These turn viewers into
# commenters, which boosts reach and converts passive readers to followers.
ENGAGEMENT_HOOKS = {
    "policy_regulation": [
        "How is this hitting your portfolio right now?",
        "Which sector do you think moves first on this?",
        "Are you adjusting any positions around this?",
        "What's your read — overreaction or the start of something bigger?",
    ],
    "geopolitics_commodities": [
        "Are you hedging energy exposure here or riding it out?",
        "Which sector feels the ripple first — transports, chemicals, or something else?",
        "How are you positioning for the next supply disruption?",
        "Is this the kind of move that shifts your Q allocation?",
    ],
    "big_business_moves": [
        "Is this a buying opportunity or a signal to step back?",
        "Which competitor benefits most from this?",
        "Does this change how you're trading the sector?",
        "Would you touch the options on this name right now?",
    ],
    "wealth_building": [
        "What's the one money lesson you wish you learned earlier?",
        "Are you doing this already or is it new to you?",
        "What's your biggest wealth-building mistake so far?",
        "Does this match your approach or would you do it differently?",
    ],
    "market_mechanics": [
        "Did you know this already or is this new info?",
        "Have you seen this play out in your own trades?",
        "Does this change how you think about market structure?",
        "What's one market mechanic most retail traders get wrong?",
    ],
    "options_insight": [
        "Have you been burned by this in your own trading?",
        "What's your go-to options strategy when IV spikes?",
        "Does this match what you see on the flow?",
        "Are you trading 0DTE or staying away? What's your edge?",
    ],
    "breaking_news": [
        "How are you trading this — or are you sitting this one out?",
        "Which direction does this push your sector exposure?",
        "Is this a one-day move or the start of a trend?",
        "What's your first trade after hearing this?",
    ],
}

# ─── Post CTA Footer (appended to every post) ─────────────────────────────
# Converts passive viewers to followers by giving them a reason to follow
# and a next step. Every post needs this — FB doesn't show page content
# twice to the same non-follower without engagement.
POST_CTA_FOOTER = (
    "\n\n📊 Follow Alpha Trading Floor for daily market signals."
    "\n💬 Real-time trade ideas → discord.gg/alphatradelevels"
    "\n📺 Full video analysis → youtube.com/@AlphaTradingFloor"
)

# ─── Poll Posts (1 per week) ───────────────────────────────────────────────
# Polls get 2-3x more engagement than text posts on FB. We alternate one
# poll into the daily schedule on Tuesdays (11:00 slot).
POLL_QUESTIONS = [
    {"question": "Fed decision next meeting — hold or cut?",
     "options": ["Hold steady", "Cut 25bps", "Cut 50bps or more"]},
    {"question": "If you had to pick one sector for the next 3 months, which is it?",
     "options": ["Tech / AI", "Energy / Commodities", "Healthcare / Biotech", "Financials / Banks"]},
    {"question": "Biggest risk to your portfolio right now?",
     "options": ["Tariffs / Trade war", "Inflation / Fed policy", "Geopolitical conflict", "Earnings misses"]},
    {"question": "How are you playing elevated VIX right now?",
     "options": ["Selling premium", "Buying protection", "Staying cash", "Buying the dip"]},
    {"question": "Which earnings name are you watching closest this week?",
     "options": ["NVDA", "TSLA", "AAPL", "Something else — comment below"]},
    {"question": "Best wealth-building move most people skip?",
     "options": ["Tax-loss harvesting", "Maxing retirement accounts", "Paying off high-rate debt", "Diversifying beyond US stocks"]},
    {"question": "What's your typical hold time on an options trade?",
     "options": ["0DTE only", "1-5 days", "1-4 weeks", "LEAPS (months)"]},
    {"question": "Oil's moving again. What are you doing?",
     "options": ["Hedging energy downside", "Buying energy calls", "Avoiding the whole sector", "Watching from the sidelines"]},
]

# --- DEBUG LOGGER ---
def debug_log(message, level="INFO"):
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] [{level}] {message}"
    print(formatted)
    with open(LOG_FILE, "a") as f:
        f.write(formatted + "\n")

# --- CLI-ONLY GPT-5.5 GENERATION ---
# --- HERMES CLI MODEL FALLBACK CHAIN ---
# The order is intentional and must remain visible in logs. All calls go through
# Hermes CLI so OAuth/provider routing stays in one place.
TEXT_MODEL_CHAIN = (
    ("GPT-5.5", CODEX_PROVIDER, CODEX_MODEL, True, CODEX_TIMEOUT_SECONDS, CODEX_RUN_BUDGET_SECONDS),
    ("Grok 4.6", GROK_TEXT_PROVIDER, GROK_TEXT_MODEL, False, FALLBACK_TIMEOUT_SECONDS, FALLBACK_RUN_BUDGET_SECONDS),
)

# Hermes can return code 0 after silently switching to a configured fallback.
# Never accept substituted output under the name of the requested provider.
PROVIDER_SUBSTITUTION_RE = re.compile(
    r"(?:primary auth failed|switching to fallback|falling back to)",
    re.IGNORECASE,
)


def resolve_llm_cli():
    """Resolve the Hermes executable used for provider-backed generation."""
    configured = HERMES_CLI.strip()
    candidates = [
        configured,
        str(Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "hermes"),
        "/usr/local/bin/hermes",
        "/opt/homebrew/bin/hermes",
    ]
    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if os.path.isabs(candidate):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        else:
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
    return None


def _build_cli_command(cli, provider, model, toolsets="", safe_mode=False, max_turns=1, run_budget=30):
    command = [cli, "chat", "-Q", "-t", toolsets]
    if safe_mode:
        command.append("--safe-mode")
    command.extend(
        [
            "--max-turns",
            str(max_turns),
            "--run-budget",
            str(run_budget),
            "--provider",
            provider,
            "-m",
            model,
            "--query-file",
            "-",
            "--source",
            "alpha-facebook",
        ]
    )
    return command


def _child_env():
    """Keep legacy routes from changing the selected provider."""
    child_env = os.environ.copy()
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "OLLAMA_API_KEY",
        "OLLAMA_MODEL",
        "OLLAMA_URL",
        "OLLAMA_PRIMARY",
        "OLLAMA_SECONDARY",
    ):
        child_env.pop(name, None)
    child_env["PYTHONUNBUFFERED"] = "1"
    return child_env


def _extract_cli_text(stdout):
    """Remove Hermes diagnostics before the result reaches caption validation."""
    lines = []
    for raw_line in (stdout or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("session_id:"):
            continue
        if re.match(r"^Warning:\s+Unknown toolsets:", line, flags=re.IGNORECASE):
            continue
        if line.startswith("⚠️") or line.startswith("⚠"):
            continue
        lines.append(line)
    return clean_fact(" ".join(lines))


def _call_model_cli(prompt, label, provider, model, safe_mode=False, timeout=45, run_budget=30):
    cli = resolve_llm_cli()
    if not cli:
        debug_log(f"{label} unavailable: Hermes CLI not found.", "ERROR")
        return None

    debug_log(f"🧠 Trying caption provider: {label} ({provider}/{model})", "INFO")
    command = _build_cli_command(
        cli,
        provider,
        model,
        toolsets="",
        safe_mode=safe_mode,
        max_turns=1,
        run_budget=run_budget,
    )
    try:
        completed = subprocess.run(
            command,
            input=prompt,
            text=True,
            capture_output=True,
            cwd=str(Path(__file__).parent),
            env=_child_env(),
            timeout=timeout,
        )
    except FileNotFoundError:
        debug_log(f"{label} failed: Hermes CLI disappeared while launching.", "ERROR")
        return None
    except subprocess.TimeoutExpired:
        debug_log(f"{label} timed out after {timeout}s; continuing fallback chain.", "WARN")
        return None
    except Exception as exc:
        debug_log(f"{label} failed to start: {type(exc).__name__}: {exc}", "ERROR")
        return None

    diagnostics = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    if PROVIDER_SUBSTITUTION_RE.search(diagnostics):
        debug_log(f"{label} was substituted by Hermes fallback; rejecting that output.", "WARN")
        return None

    if completed.returncode != 0:
        detail = re.sub(r"\s+", " ", (completed.stderr or completed.stdout or "").strip())
        debug_log(
            f"{label} CLI exited {completed.returncode}: {detail[:300] or 'no diagnostic output'}",
            "WARN",
        )
        return None

    text = _extract_cli_text(completed.stdout)
    if not text:
        debug_log(f"{label} returned empty output; continuing fallback chain.", "WARN")
        return None
    if LLM_DEBUG:
        debug_log(f"{label} response: {text[:200]}", "DEBUG")
    return text


def call_gpt_cli(prompt, model=None):
    """Compatibility wrapper for the primary GPT caption provider."""
    selected_model = model or CODEX_MODEL
    if selected_model != CODEX_MODEL:
        debug_log(
            f"Blocked non-canonical model request: {selected_model}; required {CODEX_MODEL}.",
            "ERROR",
        )
        return None
    return _call_model_cli(
        prompt,
        "GPT-5.5",
        CODEX_PROVIDER,
        CODEX_MODEL,
        safe_mode=True,
        timeout=CODEX_TIMEOUT_SECONDS,
        run_budget=CODEX_RUN_BUDGET_SECONDS,
    )


def generate_cli_only(prompt):
    """Generate through the primary GPT provider for backward compatibility."""
    return call_gpt_cli(prompt)


# --- STORAGE FUNCTIONS ---
# --- STORAGE FUNCTIONS ---
def load_posted_quotes():
    if os.path.exists(POST_HISTORY_FILE):
        with open(POST_HISTORY_FILE, "r") as f:
            try:
                return list(json.load(f))
            except Exception as e:
                debug_log(f"Could not read history file: {e}", "ERROR")
                return []
    return []


def save_posted_quotes(quotes):
    if len(quotes) > MAX_HISTORY:
        quotes = quotes[-MAX_HISTORY:]
    with open(POST_HISTORY_FILE, "w") as f:
        json.dump(quotes, f, indent=2)
    return quotes


def prune_history():
    quotes = load_posted_quotes()
    cleaned = []
    seen = set()
    for q in quotes:
        cq = clean_fact(str(q))
        if not is_valid_fact(cq):
            continue
        nq = normalize_fact(cq)
        if nq in seen:
            continue
        seen.add(nq)
        cleaned.append(cq)

    if len(cleaned) != len(quotes):
        debug_log(f"History cleaned: {len(quotes)} -> {len(cleaned)} valid unique entries", "INFO")
    if len(cleaned) > MAX_HISTORY:
        debug_log(f"Pruning history from {len(cleaned)} to {MAX_HISTORY} entries", "INFO")

    return save_posted_quotes(cleaned)


# --- DEDUP (same approach as X publisher) ---
def _normalized_similarity_text(text):
    """Normalize text for similarity comparison — numbers become <num>."""
    text = re.sub(r"\$?\d+(?:\.\d+)?%?", "<num>", text.casefold())
    return " ".join(re.findall(r"[a-z]+|<num>", text))


def is_near_duplicate(candidate, recent_posts, threshold=SIMILARITY_THRESHOLD):
    """Check if candidate is too similar to any recent post."""
    norm_candidate = _normalized_similarity_text(candidate)
    best_score = 0.0
    for recent in recent_posts:
        score = SequenceMatcher(None, norm_candidate, _normalized_similarity_text(recent)).ratio()
        best_score = max(best_score, score)
        if score >= threshold:
            return True, score
    return False, best_score


# --- TEXT NORMALIZATION ---
def normalize_fact(fact):
    return re.sub(r'\s+', ' ', fact.strip().lower())


def load_prompt_templates():
    if os.path.exists(PROMPT_FILE):
        try:
            with open(PROMPT_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, list) and data:
                    return [str(x).strip() for x in data if str(x).strip()]
        except Exception as e:
            debug_log(f"Could not read {PROMPT_FILE}: {e}", "WARN")
    return [
        "Cinematic financial visual with dramatic lighting",
        "Clean modern market infographic with bold numbers",
        "Wall Street scene with depth and atmosphere",
        "Bold wealth concept with strong color contrast",
    ]


# --- CONTENT GENERATION ---
def clean_fact(text):
    if not text:
        return ""
    fact = text.strip()
    fact = re.sub(r"^```(?:text|plain)?\s*", "", fact, flags=re.IGNORECASE)
    fact = re.sub(r"\s*```$", "", fact)
    fact = re.sub(r"^(?:caption|post|final answer)\s*:\s*", "", fact, flags=re.IGNORECASE)
    fact = fact.replace("\n", " ")
    fact = re.sub(r"\s+", " ", fact)
    # strip wrapping quotes only
    if (fact.startswith('"') and fact.endswith('"')) or (fact.startswith("'") and fact.endswith("'")):
        fact = fact[1:-1].strip()
    fact = fact.replace("**", "").replace("__", "").replace("`", "")
    return fact


DANGLING_ENDINGS = (
    "and",
    "or",
    "but",
    "because",
    "which means",
    "including",
    "such as",
    "options flow",
    "due to",
    "as a result",
)


def is_complete_caption(text):
    """Return True only when a caption ends as a complete thought."""
    value = clean_fact(text)
    if len(value) < 40 or value[-1] not in ".?!":
        return False
    if value.endswith("..."):
        return False

    lower = value.casefold().rstrip()
    without_terminal = re.sub(r"[.!?]+$", "", lower).rstrip()
    if without_terminal.endswith((":", ";", ",")):
        return False
    if any(
        without_terminal == ending or without_terminal.endswith(f" {ending}")
        for ending in DANGLING_ENDINGS
    ):
        return False
    if re.search(r"\b(?:a|an|the|to|of|for|with|on|in|from|into|as|by)[.!?]+$", lower):
        return False
    if lower.count("(") != lower.count(")") or lower.count("[") != lower.count("]"):
        return False
    return True



def is_valid_fact(fact):
    if not fact:
        return False
    lower = fact.lower().strip()

    banned_fragments = [
        "did you know",
        "here's a",
        "here are",
        "let me know",
        "#",
        "caption:",
        "fun facts",
        "multiple hashtags",
        "as an ai",
        "i'm an ai",
        "as a language model",
        "in conclusion",
        "it's important to note",
        "it is important to note",
        "keep in mind",
        "remember that",
        "in this post",
        "in today's post",
        "let's dive in",
        "stick around",
        "stay tuned",
        "follow for more",
        "like and share",
        "drop a comment",
        "loaded the alpha trading floor",
        "web search failed",
        "unavailable provider",
        "could not verify",
        "couldn't verify",
        "i could not",
        "i couldn't",
        "i was unable",
        "i cannot",
        "i can't",
        "did not produce the final",
        "final facebook caption",
        "research tool",
        "available search tool",
        "authentication failed",
        "api error",
        "http 401",
        "http 403",
    ]
    if any(b in lower for b in banned_fragments):
        return False

    # 40-600 chars for Facebook (increased to accommodate engagement hook + CTA footer)
    if len(fact) < 40 or len(fact) > 600:
        return False

    # Never publish a model response that stops mid-sentence.
    if not is_complete_caption(fact):
        return False

    # Reject actual numbered-list lines without rejecting prose such as
    # "$371. The thesis..." in a market caption.
    if re.search(r"(?m)^\s*\d+\.\s", fact) or "\n" in fact:
        return False

    # Must contain at least one substantive financial term or concept
    financial_terms = [
        "market", "stock", "trading", "invest", "money", "finance", "economic",
        "fed", "sec", "oil", "trump", "elon", "musk", "layoff", "laid off",
        "earning", "profit", "loss", "revenue", "price", "rate", "inflation",
        "interest", "bond", "option", "portfolio", "wealth", "debt", "gdp",
        "tax", "merger", "acquisition", "ipo", "dividend", "index", "sp",
        "dow", "nasdaq", "dollar", "currency", "war", "sanction", "tariff",
        "supply", "demand", "hedge", "bull", "bear", "volatility", "yield",
        "commodity", "futures", "bank", "credit", "fund", "capital",
        "job", "employ", "unemploy", "recession", "deficit", "trade",
        "target", "upgrade", "upgraded", "downgrade", "downgraded", "analyst",
        "outperform", "underperform", "guidance", "share", "shares", "volume",
        "growth", "sales", "import", "export", "opec", "crypto", "bitcoin", "real estate",
    ]
    if not any(term in lower for term in financial_terms):
        return False

    return True


def generate_content(prompt_seed=None, content_type=None):
    """Generate a high-value financial post for Alpha Trading Floor."""

    if not content_type:
        content_type = random.choice(CONTENT_PILLARS)

    pillar_instructions = {
        "policy_regulation": (
            "Write about a real policy, regulatory, or government action that impacts "
            "markets or personal finance. This includes Trump administration moves, "
            "SEC enforcement, Fed decisions, trade tariffs, tax changes, or legislation. "
            "Be specific about what happened. Then add a brief opinion on how this could "
            "impact financial markets, options volatility, and affected businesses."
        ),
        "geopolitics_commodities": (
            "Write about a geopolitics or commodities story that moves markets. "
            "This includes oil price shifts, OPEC decisions, war/conflict impact on "
            "supply chains, sanctions, trade deals, or energy transitions. "
            "Connect the event to what people should care about. Then add your take "
            "on how this impacts options flow, sector volatility, and related businesses."
        ),
        "big_business_moves": (
            "Write about a significant corporate move: major layoffs at big companies, "
            "earnings surprises, CEO changes, M&A deals, IPOs, or product launches "
            "that shake up industries. Focus on the financial impact, not just the headline. "
            "Then add your opinion on how this affects the stock, options sentiment, and "
            "competitor businesses."
        ),
        "wealth_building": (
            "Share a specific, actionable wealth-building insight. Not generic advice "
            "like 'save money' — think: specific tax strategies, compounding mechanisms, "
            "asset allocation principles that most people get wrong, or financial "
            "decisions that separate wealth-builders from everyone else."
        ),
        "market_mechanics": (
            "Explain a market mechanism or professional trading concept in plain language. "
            "How options really work, why volatility spikes, what smart money does "
            "differently, how market makers profit, or why certain patterns repeat. "
            "Make it practical and interesting, not textbook."
        ),
        "options_insight": (
            "Share a practical options or trading insight that most retail traders miss. "
            "Think: how implied volatility actually works, why gamma squeezes happen, "
            "how market makers hedge, the real cost of holding options through earnings, "
            "or why most 0DTE trades lose. Must be specific, accurate, and written "
            "like a trader explaining to another trader — not a textbook."
        ),
        "breaking_news": (
            "Write about a breaking financial news event that just happened or is "
            "unfolding right now. This could be: a major policy announcement, SEC "
            "enforcement action, unexpected Fed move, Trump executive order affecting "
            "markets, significant layoff announcement, geopolitical escalation, oil "
            "supply disruption, or major earnings miss. State what happened clearly. "
            "Then add YOUR OPINION on what this means for: financial markets (which "
            "indices/sectors move), options trading (volatility, skew, flow), and "
            "specific businesses (who wins, who loses). Sound confident and direct — "
            "like a trader giving their read, not a journalist reporting the news."
        ),
    }

    instruction = pillar_instructions.get(content_type, pillar_instructions["options_insight"])

    rules = (
        "Rules:\n"
        "- Sound like a sharp, experienced trader or investor — natural, confident, direct\n"
        "- No hashtags, no emojis, no numbered lists, no intro filler\n"
        "- Do NOT start with 'Did you know', 'Here's a', or any setup phrase\n"
        "- Start with the insight itself — the most interesting part first\n"
        "- Keep it between 80-380 characters so the final thought has room to finish\n"
        "- Return only the finished caption, never analysis or a label\n"
        "- This call has no tools. Do not invoke tools or discuss tool, provider, or research availability\n"
        "- If a current event cannot be supported by the supplied context, silently write an evergreen educational insight instead\n"
        "- Use complete sentences and end with exactly one terminal mark: period, question mark, or exclamation point\n"
        "- Never end with an unfinished phrase, conjunction, preposition, colon, ellipsis, or dangling 'options flow'\n"
        "- Be specific: real numbers, real companies, real mechanisms when possible\n"
        "- If you reference a specific event, person, or number, make it verifiable\n"
        "- Write like a human behind the keyboard, not a financial report or AI\n"
        "- No financial advice, no buy/sell instructions, no guaranteed outcomes\n"
        "- FOR BREAKING NEWS: end with your opinion on how this impacts financial markets, options, and businesses — sound like a trader giving their read\n"
        "- FOR ALL POSTS about policy, geopolitics, or business moves: include a brief opinion on financial/options impact when the content warrants it\n"
        "- OPTIONAL: ending the insight with a specific, answerable question makes it more engaging (e.g. 'What's your read on this?', 'Which sector moves first?') — but do NOT use generic filler questions like 'What do you think?'\n"
    )
    style_hint = f"- Style hint: {prompt_seed}\n" if prompt_seed else ""
    prompt = (
        "You are the content writer for Alpha Trading Floor, a financial education brand on Facebook. Write exactly ONE social post.\n\n"
        f"Content type: {content_type}\n"
        f"Focus: {instruction}\n\n"
        + rules
        + style_hint
    )

    for label, provider, model, safe_mode, timeout, run_budget in TEXT_MODEL_CHAIN:
        if provider == CODEX_PROVIDER:
            raw = generate_cli_only(prompt)
        else:
            raw = _call_model_cli(
                prompt,
                label,
                provider,
                model,
                safe_mode=safe_mode,
                timeout=timeout,
                run_budget=run_budget,
            )
        if not raw:
            continue

        fact = clean_fact(raw)
        if is_valid_fact(fact):
            debug_log(f"✅ Caption generated via {label} ({len(fact)} chars).", "INFO")
            return fact
        debug_log(f"{label} returned an invalid/incomplete caption; trying next provider.", "WARN")

    debug_log("All caption providers failed validation: GPT-5.5 -> Grok 4.6.", "ERROR")
    return None


# --- BACKGROUND IMAGE ROTATION ---

def validate_photo_asset(path):
    """Accept only a real, readable, sufficiently sized image asset."""
    if not path or not os.path.isfile(path):
        return False
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                return False
            width, height = image.size
            return width >= 640 and height >= 360
    except Exception as exc:
        debug_log(f"Photo asset validation failed: {type(exc).__name__}", "WARN")
        return False


def create_final_image(background_path, text=None):
    """Return a clean image-only post asset (no text rendered onto image)."""
    if not validate_photo_asset(background_path):
        raise ValueError("background is not a valid photo asset")
    img = Image.open(background_path).convert("RGB")
    output_dir = Path(GPT_IMAGE_DIR)
    if not output_dir.is_absolute():
        output_dir = Path(__file__).parent / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"final_{uuid.uuid4().hex[:10]}.png"
    img.save(output_path)
    if not validate_photo_asset(output_path):
        raise ValueError("rendered photo asset failed validation")
    return str(output_path)


# --- IMAGE PROVIDER FALLBACK CHAIN ---

def _save_remote_image(url, prefix):
    output_dir = Path(GROK_IMAGE_DIR if prefix.startswith("grok") else GPT_IMAGE_DIR)
    if not output_dir.is_absolute():
        output_dir = Path(__file__).parent / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{prefix}_{uuid.uuid4().hex[:10]}.png"
    try:
        response = requests.get(url, timeout=IMAGE_DOWNLOAD_TIMEOUT_SECONDS)
        if response.status_code != 200 or not response.content:
            debug_log(f"{prefix} image download failed: HTTP {response.status_code}", "WARN")
            return None
        with open(out_path, "wb") as image_file:
            image_file.write(response.content)
        if validate_photo_asset(out_path):
            return str(out_path)
        debug_log(f"{prefix} image download was not a valid photo asset.", "WARN")
    except Exception as exc:
        debug_log(f"{prefix} image download failed: {type(exc).__name__}", "WARN")
    return None


def _extract_image_reference(output):
    """Find a local MEDIA path or remote image URL in Hermes output."""
    for raw_line in (output or "").splitlines():
        line = raw_line.strip()
        if "MEDIA:" in line.upper():
            candidate = re.split(r"MEDIA:\s*", line, maxsplit=1, flags=re.IGNORECASE)[-1].strip()
            candidate = candidate.strip("`\"'()[]<>,")
            if validate_photo_asset(candidate):
                return candidate

    for candidate in re.findall(r"/[^\"\n]*?\.(?:png|jpe?g|webp)", output or "", flags=re.IGNORECASE):
        candidate = candidate.rstrip(".,;:)]}")
        if validate_photo_asset(candidate):
            return candidate

    urls = re.findall(r"https?://[^\s)\"]+", output or "", flags=re.IGNORECASE)
    for url in urls:
        url = url.rstrip(".,;:)]}")
        if re.search(r"\.(?:png|jpe?g|webp)(?:\?|$)", url, flags=re.IGNORECASE):
            saved = _save_remote_image(url, "gpt")
            if saved:
                return saved
    return None


def get_gpt_background(image_prompt):
    """Ask Hermes image_gen for a clean finance image; never publish from here."""
    cli = resolve_llm_cli()
    if not cli:
        debug_log("GPT image generation unavailable: Hermes CLI not found.", "WARN")
        return None

    debug_log("🎨 Trying GPT image generation first...", "INFO")
    request = (
        "Use the image_gen tool now to create one clean, photorealistic 16:9 finance image. "
        "No readable text, logos, screenshots, faces, watermarks, or UI. The image will be "
        "used as a regular Facebook photo attachment, not a Reel. Do not browse or publish. "
        "After the tool finishes, return only the resulting local image path or MEDIA path.\n\n"
        f"Creative direction: {image_prompt}"
    )
    command = _build_cli_command(
        cli,
        CODEX_PROVIDER,
        CODEX_MODEL,
        toolsets="image_gen",
        safe_mode=False,
        max_turns=2,
        run_budget=GPT_IMAGE_RUN_BUDGET_SECONDS,
    )
    try:
        completed = subprocess.run(
            command,
            input=request,
            text=True,
            capture_output=True,
            cwd=str(Path(__file__).parent),
            env=_child_env(),
            timeout=GPT_IMAGE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        debug_log(f"GPT image generation timed out after {GPT_IMAGE_TIMEOUT_SECONDS}s; trying Grok.", "WARN")
        return None
    except Exception as exc:
        debug_log(f"GPT image generation failed to start: {type(exc).__name__}", "WARN")
        return None

    diagnostics = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    if PROVIDER_SUBSTITUTION_RE.search(diagnostics):
        debug_log("GPT image request was substituted by Hermes fallback; rejecting it.", "WARN")
        return None
    if completed.returncode != 0:
        debug_log(f"GPT image CLI exited {completed.returncode}; trying Grok.", "WARN")
        return None

    image_path = _extract_image_reference(completed.stdout)
    if image_path:
        debug_log(f"✅ GPT image ready: {image_path}", "INFO")
        return image_path
    debug_log("GPT image tool returned no verified image path; trying Grok.", "WARN")
    return None


def _resolve_hermes_python():
    """Resolve the Hermes runtime Python used by native image plugins.

    The checked-in Hermes launcher may use its managed runtime rather than the
    legacy ``hermes-agent/venv``.  The latter can import the plugin package but
    miss runtime dependencies such as ``pm``, which makes the provider fail
    only when it resolves credentials.  Ask the active launcher for its real
    runtime command first, then keep explicit and legacy paths as fallbacks.
    """
    candidates = []
    configured = os.getenv("HERMES_PYTHON", "").strip()
    if configured:
        candidates.append(configured)

    cli = resolve_llm_cli()
    if cli:
        # ``hermes --print-runtime-command`` returns a JSON argv whose first
        # item is the managed Python executable.  This avoids hard-coding a
        # versioned Hermes runtime path.
        try:
            completed = subprocess.run(
                [cli, "--print-runtime-command"],
                text=True,
                capture_output=True,
                timeout=5,
            )
            runtime_command = json.loads(completed.stdout or "")
            if (
                completed.returncode == 0
                and isinstance(runtime_command, list)
                and runtime_command
                and isinstance(runtime_command[0], str)
            ):
                candidates.append(runtime_command[0])
        except (OSError, subprocess.SubprocessError, TypeError, ValueError):
            pass
        candidates.append(str(Path(cli).with_name("python")))
        candidates.append(str(Path(cli).with_name("python3")))

    candidates.append(str(Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"))
    for candidate in dict.fromkeys(candidates):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _extract_json_result(output):
    """Find the last JSON object emitted by a provider helper."""
    for raw_line in reversed((output or "").splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and "success" in payload:
            return payload
    return None


def get_grok_background(image_prompt):
    """Generate through native xAI Grok Imagine, not the unauthenticated web UI."""
    if not GROK_ENABLED:
        return None

    helper = Path(XAI_IMAGE_HELPER)
    hermes_python = _resolve_hermes_python()
    if not helper.is_file() or not hermes_python:
        debug_log("Native xAI image helper is unavailable; using local photo fallback.", "WARN")
        return None

    debug_log(f"🎨 Trying native xAI image generation ({XAI_IMAGE_MODEL})...", "INFO")
    try:
        completed = subprocess.run(
            [hermes_python, str(helper)],
            input=json.dumps({
                "prompt": image_prompt,
                "model": XAI_IMAGE_MODEL,
                "aspect_ratio": "landscape",
            }),
            text=True,
            capture_output=True,
            cwd=str(helper.parent),
            env=_child_env(),
            timeout=GROK_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        debug_log(f"Native xAI image generation timed out after {GROK_TIMEOUT_SECONDS}s.", "WARN")
        return None
    except Exception as exc:
        debug_log(f"Native xAI image generation failed to start: {type(exc).__name__}.", "WARN")
        return None

    result = _extract_json_result(completed.stdout)
    if completed.returncode != 0 or not result or not result.get("success"):
        error_type = result.get("error_type", "provider_error") if result else "invalid_provider_output"
        debug_log(f"Native xAI image generation failed ({error_type}).", "WARN")
        return None

    image_ref = str(result.get("image") or result.get("public_url") or "").strip()
    if validate_photo_asset(image_ref):
        debug_log(f"✅ Native xAI image ready: {image_ref}", "INFO")
        return image_ref
    if image_ref.lower().startswith(("http://", "https://")):
        saved = _save_remote_image(image_ref, "grok")
        if saved:
            debug_log(f"✅ Native xAI image downloaded: {saved}", "INFO")
        return saved
    debug_log("Native xAI returned no usable image reference.", "WARN")
    return None


def _background_rotation_path():
    path = Path(BACKGROUND_ROTATION_FILE)
    return path if path.is_absolute() else Path(__file__).parent / path


def _load_background_rotation():
    try:
        data = json.loads(_background_rotation_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _save_background_rotation(filename):
    path = _background_rotation_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_file": filename}, indent=2) + "\n", encoding="utf-8")


def _next_rotated_filename(files, last_file):
    ordered = sorted(files, key=str.casefold)
    if not ordered:
        return None
    if last_file in ordered:
        start = (ordered.index(last_file) + 1) % len(ordered)
        return ordered[start]
    return ordered[0]


def get_local_background():
    bg_folder = "backgrounds"
    used_folder = os.path.join(bg_folder, "used")

    os.makedirs(bg_folder, exist_ok=True)
    os.makedirs(used_folder, exist_ok=True)

    files = [
        f for f in os.listdir(bg_folder)
        if not f.startswith(".") and f.lower().endswith((".png", ".jpg", ".jpeg"))
    ]

    remaining = len(files)
    last_file = str(_load_background_rotation().get("last_file", ""))

    if remaining == 0:
        # Never recycle a published background. The caller may still try a
        # bounded image provider, but local exhaustion must fail closed rather
        # than silently repeating a prior Facebook picture.
        debug_log(
            "No unused backgrounds remain; refusing to reuse a prior Facebook picture.",
            "WARN",
        )
        return None

    chosen_file = _next_rotated_filename(files, last_file)
    source_path = os.path.join(bg_folder, chosen_file)
    destination_path = os.path.join(used_folder, chosen_file)

    debug_log(f"🎨 Selected background: {chosen_file} (remaining before pick = {remaining})", "INFO")

    if DRY_RUN:
        debug_log(f"[DRY RUN] Selected background without rotating: {chosen_file}", "INFO")
        return source_path

    try:
        shutil.move(source_path, destination_path)
        debug_log(f"Moved background to used/: {chosen_file}", "DEBUG")
    except Exception as e:
        debug_log(f"Error moving background {chosen_file}: {e}", "ERROR")
        return source_path

    _save_background_rotation(chosen_file)
    return destination_path


def _read_gpt_circuit_state():
    """Read GPT image circuit-breaker state."""
    try:
        with open(GPT_IMAGE_CIRCUIT_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"consecutive_failures": 0, "opened_at": None}


def _write_gpt_circuit_state(state):
    try:
        with open(GPT_IMAGE_CIRCUIT_FILE, "w") as f:
            json.dump(state, f)
    except OSError as exc:
        debug_log(f"GPT circuit state write failed: {type(exc).__name__}", "WARN")


def _gpt_circuit_open():
    """Return True if the GPT image provider should be skipped (circuit open)."""
    state = _read_gpt_circuit_state()
    failures = state.get("consecutive_failures", 0)
    if failures < GPT_IMAGE_CIRCUIT_THRESHOLD:
        return False
    opened_at = state.get("opened_at")
    if not opened_at:
        return True
    try:
        opened = datetime.fromisoformat(opened_at)
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=UTC)
        elapsed = datetime.now(UTC) - opened
        if elapsed.total_seconds() >= GPT_IMAGE_CIRCUIT_COOLDOWN_HOURS * 3600:
            return False  # cooldown elapsed — allow one probe attempt
    except (ValueError, TypeError):
        pass
    return True


def _record_gpt_result(success):
    """Update GPT circuit state after an attempt."""
    state = _read_gpt_circuit_state()
    if success:
        state["consecutive_failures"] = 0
        state["opened_at"] = None
    else:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        if state["consecutive_failures"] >= GPT_IMAGE_CIRCUIT_THRESHOLD:
            state["opened_at"] = datetime.now(UTC).isoformat()
    _write_gpt_circuit_state(state)


def generate_background_asset(image_prompt, text=None):
    """Run the image provider chain once and return one unique verified asset."""
    gpt_skipped = _gpt_circuit_open()
    for label, generator in (
        ("GPT Image 2", get_gpt_background),
        ("native xAI Grok Imagine", get_grok_background),
    ):
        if label == "GPT Image 2" and gpt_skipped:
            debug_log("GPT image circuit open — skipping to Grok.", "WARN")
            continue
        candidate = generator(image_prompt)
        valid = bool(candidate) and validate_photo_asset(candidate)
        if label == "GPT Image 2":
            _record_gpt_result(valid)
        if not valid:
            continue
        try:
            final_path = create_final_image(candidate, text)
            debug_log(f"✅ Selected {label} background: {final_path}", "INFO")
            return final_path
        except Exception as exc:
            debug_log(f"{label} asset failed final validation: {type(exc).__name__}; trying next provider.", "WARN")

    local_path = get_local_background()
    if local_path and validate_photo_asset(local_path):
        try:
            final_path = create_final_image(local_path, text)
            debug_log(f"Selected rotated local photo fallback: {final_path}", "WARN")
            return final_path
        except Exception as exc:
            debug_log(f"Local photo fallback failed: {type(exc).__name__}", "ERROR")
    return None


# --- FACEBOOK POST (PHOTO + CAPTION — NEVER TEXT-ONLY OR REELS) ---
def _facebook_error_detail(response):
    """Return safe Graph diagnostics without logging response bodies or tokens."""
    try:
        data = response.json()
    except Exception:
        data = {}
    error = data.get("error", {}) if isinstance(data, dict) else {}
    if isinstance(error, dict):
        code = error.get("code", "unknown")
        message = error.get("message", "unknown Graph error")
        return f"HTTP {response.status_code}, code={code}, message={message}"
    return f"HTTP {response.status_code}, unexpected Graph response"


def verify_facebook_page_access():
    """Verify the configured Page token and identity before any public write."""
    if not FACEBOOK_PAGE_ACCESS_TOKEN or not FACEBOOK_PAGE_ID:
        debug_log("Missing FACEBOOK_PAGE_ACCESS_TOKEN or FACEBOOK_PAGE_ID in .env", "ERROR")
        return False

    url = f"https://graph.facebook.com/{FACEBOOK_PAGE_ID}"
    try:
        response = requests.get(
            url,
            params={
                "fields": "id,name",
                "access_token": FACEBOOK_PAGE_ACCESS_TOKEN,
            },
            timeout=FACEBOOK_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        debug_log(f"Facebook auth check failed before write: {type(exc).__name__}: {exc}", "ERROR")
        return False

    if not 200 <= response.status_code < 300:
        debug_log(f"Facebook auth check failed: {_facebook_error_detail(response)}", "ERROR")
        return False

    try:
        data = response.json()
    except Exception as exc:
        debug_log(f"Facebook auth check returned invalid JSON: {type(exc).__name__}", "ERROR")
        return False

    if str(data.get("id", "")) != str(FACEBOOK_PAGE_ID):
        debug_log("Facebook auth check failed: Page ID does not match configured Page.", "ERROR")
        return False
    return True


def verify_facebook_publication(message):
    """Read back the exact caption and attached photo without retrying writes."""
    if not POST_VERIFY_ENABLED:
        return {"verified": False, "reason": "disabled"}

    deadline = time.monotonic() + POST_VERIFY_TIMEOUT_SECONDS
    fields = "id,message,created_time,permalink_url,full_picture"
    while time.monotonic() < deadline:
        for edge in ("published_posts", "feed"):
            remaining = max(1.0, deadline - time.monotonic())
            try:
                response = requests.get(
                    f"https://graph.facebook.com/{FACEBOOK_PAGE_ID}/{edge}",
                    params={
                        "fields": fields,
                        "limit": "100",
                        "access_token": FACEBOOK_PAGE_ACCESS_TOKEN,
                    },
                    timeout=min(float(FACEBOOK_TIMEOUT_SECONDS), remaining),
                )
                if not 200 <= response.status_code < 300:
                    continue
                data = response.json()
                if isinstance(data, dict) and data.get("error"):
                    continue
                for item in data.get("data", []):
                    if item.get("message", "").strip() != message.strip():
                        continue
                    if not item.get("full_picture"):
                        continue
                    return {
                        "verified": True,
                        "edge": edge,
                        "post_id": item.get("id"),
                        "created_time": item.get("created_time"),
                        "permalink_url": item.get("permalink_url"),
                    }
            except Exception:
                continue

        time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))

    return {"verified": False, "reason": "exact caption and photo not visible before timeout"}


def post_to_facebook(message, image_path=None):
    """Publish one verified regular Facebook photo post; never text-only or Reel."""
    if not is_valid_fact(message):
        debug_log("BLOCKED: refusing to publish incomplete or invalid caption.", "ERROR")
        return False

    if not image_path or not validate_photo_asset(image_path):
        debug_log("BLOCKED: refusing to publish without a verified photo asset.", "ERROR")
        return False

    if POST_AS_REEL:
        debug_log("BLOCKED: POST_AS_REEL is disabled — this page does not post reels.", "ERROR")
        return False

    if DRY_RUN:
        debug_log(f"[DRY RUN] Would post photo ({len(message)} chars): {message}", "INFO")
        return True

    if not verify_facebook_page_access():
        return False

    url = f"https://graph.facebook.com/{FACEBOOK_PAGE_ID}/photos"
    try:
        with open(image_path, "rb") as img_file:
            files = {"source": img_file}
            payload = {"caption": message, "access_token": FACEBOOK_PAGE_ACCESS_TOKEN}
            response = requests.post(
                url,
                files=files,
                data=payload,
                timeout=FACEBOOK_TIMEOUT_SECONDS,
            )

        if FB_DEBUG:
            debug_log(f"Facebook photo response status={response.status_code}", "DEBUG")

        if 200 <= response.status_code < 300:
            verification = verify_facebook_publication(message)
            if verification.get("verified"):
                debug_log(
                    f"✅ Successfully posted and verified photo to Facebook ({verification.get('post_id')}).",
                    "INFO",
                )
            else:
                # The write may already be public even when Graph readback is delayed.
                # Treat it as accepted and update cooldown/history; never retry blindly.
                debug_log(
                    "⚠️ Facebook accepted the photo write, but public readback was ambiguous; no retry attempted.",
                    "WARN",
                )
            return True

        debug_log(f"❌ Facebook photo write failed: {_facebook_error_detail(response)}", "ERROR")
        return False

    except Exception as exc:
        debug_log(f"Facebook photo post error: {type(exc).__name__}: {exc}", "ERROR")
        return False


def post_poll_to_facebook(question, options):
    """Publish a Facebook poll post. Polls get 2-3x more engagement than text posts."""
    if not question or len(options) < 2:
        debug_log("BLOCKED: poll needs a question and at least 2 options.", "ERROR")
        return False

    if DRY_RUN:
        debug_log(f"[DRY RUN] Would post poll: {question} | Options: {options}", "INFO")
        return True

    if not verify_facebook_page_access():
        return False

    # Facebook Graph API: publish a poll as a post with options
    url = f"https://graph.facebook.com/{FACEBOOK_PAGE_ID}/feed"
    try:
        # Build the poll as a text post with numbered options.
        # FB deprecated native polls for pages in 2024, so we format as
        # an interactive question post that drives comments.
        poll_text = f"{question}\n\n"
        for i, opt in enumerate(options, 1):
            poll_text += f"{i}. {opt}\n"
        poll_text += "\nVote with your number or drop your answer below."
        poll_text += POST_CTA_FOOTER

        payload = {
            "message": poll_text,
            "access_token": FACEBOOK_PAGE_ACCESS_TOKEN,
        }
        response = requests.post(url, data=payload, timeout=FACEBOOK_TIMEOUT_SECONDS)

        if FB_DEBUG:
            debug_log(f"Facebook poll response status={response.status_code}", "DEBUG")

        if 200 <= response.status_code < 300:
            data = response.json()
            post_id = data.get("id", "unknown")
            debug_log(f"✅ Successfully posted poll to Facebook ({post_id}).", "INFO")
            return True

        debug_log(f"❌ Facebook poll write failed: {_facebook_error_detail(response)}", "ERROR")
        return False

    except Exception as exc:
        debug_log(f"Facebook poll post error: {type(exc).__name__}: {exc}", "ERROR")
        return False


# --- MAIN JOB ---


def poll_job():
    """Post a poll question to Facebook. Runs once per week on Tuesday 11:00."""
    debug_log("📊 Alpha Trading Floor poll job — posting weekly engagement poll...", "INFO")

    if not can_post_now():
        return None

    poll = random.choice(POLL_QUESTIONS)
    success = post_poll_to_facebook(poll["question"], poll["options"])

    if success and not DRY_RUN:
        history = load_posted_quotes() if os.path.exists(POST_HISTORY_FILE) else []
        history.append(f"POLL: {poll['question']}")
        save_posted_quotes(history)
        record_post_time()
        debug_log("💾 Poll saved to history. Cooldown timer reset.", "INFO")

    return success


def job():
    debug_log("🚀 Alpha Trading Floor watchdog tick — checking for post-worthy content...", "INFO")

    # Enforce cooldown between posts
    if not can_post_now():
        return None

    # Dry runs must be observational: do not rewrite history while preparing
    # a candidate. Production runs still prune malformed/duplicate entries.
    history = load_posted_quotes() if DRY_RUN else prune_history()
    normalized_history = set(normalize_fact(q) for q in history)

    # Recent posts for near-duplicate check (last 20)
    recent_posts = history[-20:] if len(history) >= 20 else history

    prompt_templates = load_prompt_templates()
    prompt_seed = random.choice(prompt_templates)
    content_type = random.choice(CONTENT_PILLARS)
    debug_log(f"🧠 Content pillar: {content_type} | Prompt seed: {prompt_seed}", "INFO")

    fact = None
    for attempt in range(2):
        candidate = generate_content(prompt_seed, content_type)
        if candidate:
            normalized_candidate = normalize_fact(candidate)
            if normalized_candidate in normalized_history:
                debug_log(f"Attempt {attempt + 1}: exact duplicate, retrying fallback chain...", "WARN")
                continue

            is_dup, score = is_near_duplicate(candidate, recent_posts)
            if is_dup:
                debug_log(f"Attempt {attempt + 1}: near-duplicate ({score:.0%} similar), retrying fallback chain...", "WARN")
                continue

            fact = candidate
            break
        else:
            debug_log(
                f"Attempt {attempt + 1}: all caption providers failed or returned invalid content.",
                "WARN",
            )

    if not fact:
        debug_log(
            "Could not generate unique complete content after 2 GPT -> Grok attempts.",
            "ERROR",
        )
        return False

    debug_log(f"✍️ Generated content ({len(fact)} chars): {fact}", "INFO")

    # ── Append engagement hook question (category-specific) ────────────
    # FB algorithm weights comments 6x more than likes. A specific question
    # converts passive readers into commenters, which boosts reach.
    hooks = ENGAGEMENT_HOOKS.get(content_type, ENGAGEMENT_HOOKS.get("market_mechanics", []))
    if hooks:
        hook = random.choice(hooks)
        fact = f"{fact}\n\n{hook}"

    # ── Append CTA footer (follow + Discord + YouTube) ─────────────────
    # Converts viewers to followers by giving them a reason and a next step.
    fact = f"{fact}{POST_CTA_FOOTER}"

    # ── Build image prompt with finance theme ─────────────────────────────
    image_prompt = f"{prompt_seed} Financial concept: {fact[:100]}"
    if DRY_RUN:
        debug_log("[DRY RUN] Skipping persistent image prompt write.", "INFO")
    else:
        try:
            with open(IMAGE_PROMPT_LOG, "w") as f:
                f.write(image_prompt)
            debug_log(f"🖼️ Image prompt saved: {IMAGE_PROMPT_LOG}", "INFO")
        except Exception as exc:
            debug_log(f"Could not write image prompt log: {type(exc).__name__}: {exc}", "WARN")

    img_path = generate_background_asset(image_prompt, fact)
    if not img_path:
        debug_log("No verified generated or rotated local background available; refusing text-only publication.", "ERROR")
        return False

    if not post_to_facebook(fact, img_path):
        debug_log("Facebook publication failed; history and cooldown were not updated.", "ERROR")
        return False

    if DRY_RUN:
        debug_log("[DRY RUN] Skipping history and cooldown writes.", "INFO")
        return True

    history.append(fact)
    save_posted_quotes(history)
    record_post_time()
    debug_log("💾 Content saved to history. Cooldown timer reset.", "INFO")
    return True


# --- COOLDOWN CHECK ---
def can_post_now():
    """Check if enough time has passed since the last post."""
    if not os.path.exists(LAST_POST_FILE):
        return True
    try:
        last_str = Path(LAST_POST_FILE).read_text().strip()
        last_time = datetime.fromisoformat(last_str)
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=UTC)
        elapsed_hours = (datetime.now(UTC) - last_time).total_seconds() / 3600
        if elapsed_hours < MIN_HOURS_BETWEEN_POSTS:
            debug_log(f"Cooldown: {elapsed_hours:.1f}h since last post (need {MIN_HOURS_BETWEEN_POSTS}h). Skipping.", "INFO")
            return False
        return True
    except Exception as e:
        debug_log(f"Could not read last post time: {e}. Allowing post.", "WARN")
        return True


def record_post_time():
    """Save the current time as the last post time."""
    try:
        Path(LAST_POST_FILE).write_text(datetime.now(UTC).isoformat())
    except Exception as e:
        debug_log(f"Could not save last post time: {e}", "WARN")


# --- DUAL SCHEDULER: daily posts + watchdog ---
def main():
    """Dual-mode scheduler:
    1. DAILY POSTS: fixed times matching X schedule (09:00, 11:00, 13:00 weekdays)
       — always posts a market update / financial insight at these times
    2. WATCHDOG: polls every 30min between scheduled posts for breaking news
       — only posts if cooldown has elapsed AND there's fresh content
    Both respect the same cooldown timer so they don't double-post.
    """
    # Schedule daily posts at fixed times (same as X, weekdays only)
    for t in DAILY_POST_TIMES:
        schedule.every().monday.at(t).do(job)
        schedule.every().tuesday.at(t).do(job)
        schedule.every().wednesday.at(t).do(job)
        schedule.every().thursday.at(t).do(job)
        schedule.every().friday.at(t).do(job)
        debug_log(f"📅 Scheduled daily post: weekdays at {t}", "INFO")

    # Weekly engagement poll on Tuesday 11:00 (replaces the regular 11:00 post)
    # Polls get 2-3x more engagement than text posts on FB.
    poll_time = DAILY_POST_TIMES[1] if len(DAILY_POST_TIMES) > 1 else "11:00"
    schedule.every().tuesday.at(poll_time).do(poll_job)
    debug_log(f"📊 Scheduled weekly poll: Tuesday at {poll_time}", "INFO")

    mode_info = f"Daily posts: weekdays at {', '.join(DAILY_POST_TIMES)}"
    if WATCHDOG_ENABLED:
        mode_info += f" | Watchdog: every {POLL_INTERVAL_MIN}min for breaking news"
    debug_log(f"🔒 Sealed flow: {FLOW_ID} | {mode_info}", "INFO")
    debug_log(f"⏰ Alpha Trading Floor started — {mode_info} | min {MIN_HOURS_BETWEEN_POSTS}h cooldown. Ctrl+C to stop.", "INFO")

    if TEST_MODE:
        debug_log("🧪 TEST_MODE enabled — running one job immediately.", "INFO")
        outcome = job()
        return 0 if outcome is not False else 1

    try:
        while True:
            schedule.run_pending()
            # Between scheduled posts, run watchdog ticks for breaking news
            if WATCHDOG_ENABLED:
                job()
                time.sleep(POLL_INTERVAL_MIN * 60)
            else:
                time.sleep(30)
    except KeyboardInterrupt:
        debug_log("🛑 Stopped manually.", "INFO")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
