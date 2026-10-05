"""Diagnostic script for DeepSeek API authentication issues.

Run from project root:
    python harness/debug_deepseek_auth.py
"""

import json
import os
import sys
import time

# Load .env before anything else
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=False)
except ImportError:
    pass  # no dotenv — env vars must be set externally

import requests

SEP = "-" * 56
PASS = "[PASS]"
FAIL = "[FAIL]"
INFO = "[INFO]"
WARN = "[WARN]"


def section(title: str) -> None:
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


# ─────────────────────────────────────────────────────────────
# A) Environment variable check
# ─────────────────────────────────────────────────────────────
section("A) ENVIRONMENT VARIABLE CHECK")

deepseek_key = os.getenv("DEEPSEEK_API_KEY", "")
anthropic_key = os.getenv("CLAUDE_API_KEY", "") or os.getenv("ANTHROPIC_API_KEY", "")

if deepseek_key:
    preview = deepseek_key[:10] + "..." if len(deepseek_key) > 10 else "(too short to preview)"
    print(f"{PASS} DEEPSEEK_API_KEY is set in environment")
    print(f"{INFO}   Preview : {preview}")
    print(f"{INFO}   Length  : {len(deepseek_key)} chars")
    if deepseek_key.startswith("sk-"):
        print(f"{PASS}   Format  : starts with 'sk-' (expected)")
    else:
        print(f"{WARN}   Format  : does NOT start with 'sk-' (unexpected — may cause 401)")
    if " " in deepseek_key or "\t" in deepseek_key or "\n" in deepseek_key:
        print(f"{FAIL}   Whitespace detected in key — this WILL cause 401")
    else:
        print(f"{PASS}   No stray whitespace detected")
else:
    print(f"{FAIL} DEEPSEEK_API_KEY is NOT set (or empty)")
    print(f"{INFO}   Check your .env file contains: DEEPSEEK_API_KEY=sk-...")
    print(f"{INFO}   Skipping API tests — no key to test with")

if anthropic_key:
    print(f"{PASS} CLAUDE_API_KEY / ANTHROPIC_API_KEY is set (Haiku fallback available)")
else:
    print(f"{WARN} CLAUDE_API_KEY not set (Haiku fallback would also fail)")


# ─────────────────────────────────────────────────────────────
# B) Request header construction
# ─────────────────────────────────────────────────────────────
section("B) REQUEST HEADER CONSTRUCTION")

if deepseek_key:
    auth_header = f"Bearer {deepseek_key}"
    print(f"{PASS} Authorization header built: Bearer {deepseek_key[:10]}...")
    if "\n" in auth_header or "\r" in auth_header:
        print(f"{FAIL}   Header contains newline — this WILL cause 401/400")
    else:
        print(f"{PASS}   Header contains no illegal characters")
else:
    print(f"{FAIL} Cannot build auth header — key missing")


# ─────────────────────────────────────────────────────────────
# C) Live API request to DeepSeek
# ─────────────────────────────────────────────────────────────
section("C) LIVE API REQUEST (minimal payload)")

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

if deepseek_key:
    headers = {
        "Authorization": f"Bearer {deepseek_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "Say exactly: OK"}],
        "max_tokens": 5,
    }
    print(f"{INFO} POST {DEEPSEEK_URL}")
    print(f"{INFO} Model: deepseek-chat (cheapest probe; not deepseek-v4-flash yet)")
    t0 = time.monotonic()
    try:
        resp = requests.post(DEEPSEEK_URL, headers=headers, json=body, timeout=15)
        elapsed = (time.monotonic() - t0) * 1000

        print(f"{INFO} Status  : {resp.status_code}  ({elapsed:.0f} ms)")
        print(f"{INFO} Headers : {dict(resp.headers)}")

        try:
            body_json = resp.json()
            print(f"{INFO} Body    : {json.dumps(body_json, indent=2)[:800]}")
        except Exception:
            print(f"{INFO} Body    : {resp.text[:400]}")

        if resp.status_code == 200:
            print(f"{PASS} deepseek-chat request SUCCEEDED — API key is valid")
        elif resp.status_code == 401:
            print(f"{FAIL} 401 Unauthorized — API key rejected by DeepSeek")
            print(f"{INFO}   Common causes:")
            print(f"{INFO}     • Key is invalid or revoked")
            print(f"{INFO}     • Key has stray whitespace or newline in .env")
            print(f"{INFO}     • .env uses CRLF line endings — strip them")
            print(f"{INFO}     • Key was generated for a different DeepSeek endpoint")
        elif resp.status_code == 404:
            print(f"{WARN} 404 — model 'deepseek-chat' not found; trying deepseek-v4-flash next")
        elif resp.status_code == 429:
            print(f"{WARN} 429 Rate limited — key IS valid (rate limit proves auth works)")
        else:
            print(f"{WARN} Unexpected status {resp.status_code}")

    except requests.Timeout:
        print(f"{FAIL} Request timed out after 15 s — check network / firewall")
    except requests.ConnectionError as exc:
        print(f"{FAIL} Connection error: {exc}")
else:
    print(f"{FAIL} Skipped — no DEEPSEEK_API_KEY")


# ─────────────────────────────────────────────────────────────
# C2) Retry with deepseek-v4-flash specifically
# ─────────────────────────────────────────────────────────────
section("C2) LIVE API REQUEST (deepseek-v4-flash — actual judge model)")

if deepseek_key:
    headers = {
        "Authorization": f"Bearer {deepseek_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "Say exactly: OK"}],
        "max_tokens": 5,
    }
    print(f"{INFO} POST {DEEPSEEK_URL}")
    print(f"{INFO} Model: deepseek-v4-flash")
    t0 = time.monotonic()
    try:
        resp = requests.post(DEEPSEEK_URL, headers=headers, json=body, timeout=15)
        elapsed = (time.monotonic() - t0) * 1000
        print(f"{INFO} Status  : {resp.status_code}  ({elapsed:.0f} ms)")
        try:
            body_json = resp.json()
            print(f"{INFO} Body    : {json.dumps(body_json, indent=2)[:800]}")
        except Exception:
            print(f"{INFO} Body    : {resp.text[:400]}")

        if resp.status_code == 200:
            print(f"{PASS} deepseek-v4-flash request SUCCEEDED")
        elif resp.status_code == 404:
            print(f"{FAIL} 404 — 'deepseek-v4-flash' model ID not recognised by the API")
            print(f"{INFO}   The model ID used in judge_grader.py may be wrong.")
            print(f"{INFO}   Check DeepSeek docs for the correct model identifier.")
        elif resp.status_code == 401:
            print(f"{FAIL} 401 — auth failed for deepseek-v4-flash too")
        else:
            print(f"{WARN} Unexpected status {resp.status_code}")

    except requests.Timeout:
        print(f"{FAIL} Request timed out after 15 s")
    except requests.ConnectionError as exc:
        print(f"{FAIL} Connection error: {exc}")
else:
    print(f"{FAIL} Skipped — no DEEPSEEK_API_KEY")


# ─────────────────────────────────────────────────────────────
# D) Haiku fallback verification
# ─────────────────────────────────────────────────────────────
section("D) HAIKU FALLBACK VERIFICATION")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

if anthropic_key:
    headers_hk = {
        "x-api-key": anthropic_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body_hk = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 5,
        "messages": [{"role": "user", "content": "Say exactly: OK"}],
    }
    print(f"{INFO} POST {ANTHROPIC_URL}")
    t0 = time.monotonic()
    try:
        resp = requests.post(ANTHROPIC_URL, headers=headers_hk, json=body_hk, timeout=15)
        elapsed = (time.monotonic() - t0) * 1000
        print(f"{INFO} Status  : {resp.status_code}  ({elapsed:.0f} ms)")
        if resp.status_code == 200:
            print(f"{PASS} Haiku request SUCCEEDED — fallback will work")
        elif resp.status_code == 401:
            print(f"{FAIL} Haiku 401 — CLAUDE_API_KEY is also invalid")
        else:
            print(f"{WARN} Haiku status {resp.status_code}: {resp.text[:200]}")
    except requests.Timeout:
        print(f"{FAIL} Haiku request timed out")
    except requests.ConnectionError as exc:
        print(f"{FAIL} Haiku connection error: {exc}")
else:
    print(f"{WARN} Skipped — CLAUDE_API_KEY not set")


# ─────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────
section("SUMMARY & NEXT STEPS")

if not deepseek_key:
    print("• DEEPSEEK_API_KEY missing from environment.")
    print("  Add it to your .env file:  DEEPSEEK_API_KEY=sk-...")
else:
    print("• If you saw 401 on deepseek-chat AND deepseek-v4-flash:")
    print("  → Key is invalid. Regenerate at https://platform.deepseek.com/api_keys")
    print()
    print("• If you saw 200 on deepseek-chat but 404 on deepseek-v4-flash:")
    print("  → Model ID is wrong. Update _DEEPSEEK_MODEL in judge_grader.py")
    print("  → Check available models at: https://platform.deepseek.com/api-docs")
    print()
    print("• If you saw 200 on both:")
    print("  → API key is valid. The 401 in the harness has another cause.")
    print("  → Check that judge_grader.py reads DEEPSEEK_API_KEY (not DEEPSEEK_KEY etc)")
    print()
    print("• If you saw stray whitespace warning in section A:")
    print("  → Open .env in a hex editor or run:")
    print("    python -c \"import os; k=os.getenv('DEEPSEEK_API_KEY',''); print(repr(k))\"")
print()
