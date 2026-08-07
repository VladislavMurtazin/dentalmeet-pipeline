"""Lazy website signal scraping for cold-email personalization.

Research does NOT scrape. Send scrapes only the N leads about to go out,
in parallel (aiohttp + semaphore), with sheet + local JSON cache.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import httpx
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / "data" / "signal_cache.json"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 10
DEFAULT_CONCURRENCY = 10


def parse_signal(signal: str) -> dict[str, str]:
    """Split 'Page: … | H1: … | Meta: …' into a dict."""
    out = {"Page": "", "H1": "", "Meta": ""}
    if not signal:
        return out
    for chunk in signal.split(" | "):
        if ": " not in chunk:
            continue
        key, val = chunk.split(": ", 1)
        out[key.strip()] = " ".join(val.split())
    return out


def _normalize_domain(domain_or_url: str) -> str:
    raw = (domain_or_url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = urlparse(raw).netloc or ""
        return host.lower().removeprefix("www.")
    except Exception:
        return ""


def _url_for_domain(domain: str) -> str:
    d = _normalize_domain(domain)
    return f"https://{d}" if d else ""


def _signal_from_html(html: str) -> str:
    s = BeautifulSoup(html, "html.parser")
    title = s.title.get_text(strip=True) if s.title else ""
    h1 = s.h1.get_text(strip=True) if s.h1 else ""
    meta = s.find("meta", attrs={"name": "description"})
    desc = meta["content"].strip() if meta and meta.get("content") else ""
    return f"Page: {title} | H1: {h1} | Meta: {desc}"


def _load_cache() -> dict[str, str]:
    if not CACHE_PATH.exists():
        return {}
    try:
        raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict[str, str]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps(cache, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def cache_get(domain: str) -> str:
    key = _normalize_domain(domain)
    if not key:
        return ""
    return (_load_cache().get(key) or "").strip()


def cache_set(domain: str, signal: str) -> None:
    key = _normalize_domain(domain)
    if not key:
        return
    cache = _load_cache()
    cache[key] = signal or ""
    _save_cache(cache)


def fetch_signal(url: str) -> str:
    """Sync fetch (single URL). Retries once on timeout; never raises."""
    if not (url or "").strip():
        return ""
    target = url if "://" in url else f"https://{url}"
    headers = {"User-Agent": DEFAULT_UA}
    for attempt in range(2):
        try:
            r = httpx.get(
                target,
                follow_redirects=True,
                timeout=REQUEST_TIMEOUT,
                headers=headers,
            )
            r.raise_for_status()
            return _signal_from_html(r.text)
        except httpx.TimeoutException:
            if attempt == 0:
                continue
            print(f"WARN: signal timeout: {target}")
            return ""
        except Exception as e:
            print(f"WARN: signal failed {target}: {e}")
            return ""
    return ""


async def _fetch_one(
    session: aiohttp.ClientSession,
    domain: str,
    sem: asyncio.Semaphore,
) -> tuple[str, str]:
    """Fetch one domain; retry once on timeout. Returns (domain, signal)."""
    key = _normalize_domain(domain)
    url = _url_for_domain(key)
    if not url:
        return domain, ""

    async with sem:
        for attempt in range(2):
            try:
                async with session.get(url, allow_redirects=True) as resp:
                    # Don't fail hard on 4xx/5xx — still try to parse body
                    html = await resp.text(errors="ignore")
                    return key, _signal_from_html(html)
            except asyncio.TimeoutError:
                if attempt == 0:
                    continue
                print(f"WARN: signal timeout: {url}")
                return key, ""
            except aiohttp.ClientError as e:
                if attempt == 0 and "timeout" in str(e).lower():
                    continue
                print(f"WARN: signal failed {url}: {e}")
                return key, ""
            except Exception as e:
                print(f"WARN: signal failed {url}: {e}")
                return key, ""
    return key, ""


async def fetch_signals_parallel(
    domains: list[str],
    limit: int = DEFAULT_CONCURRENCY,
) -> dict[str, str]:
    """Scrape many domains concurrently (semaphore=limit).

    Returns ``{normalized_domain: signal}``. Failed domains map to "".
    """
    # Unique normalized domains, preserve order
    seen: set[str] = set()
    unique: list[str] = []
    for d in domains:
        key = _normalize_domain(d)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(key)

    if not unique:
        return {}

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT + 2, sock_connect=REQUEST_TIMEOUT)
    sem = asyncio.Semaphore(max(1, int(limit)))
    headers = {"User-Agent": DEFAULT_UA}

    t0 = time.perf_counter()
    results: dict[str, str] = {}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        tasks = [_fetch_one(session, d, sem) for d in unique]
        for coro in asyncio.as_completed(tasks):
            try:
                key, signal = await coro
                results[key] = signal or ""
            except Exception as e:
                print(f"WARN: parallel signal task crashed: {e}")

    elapsed = time.perf_counter() - t0
    print(
        f"Scraped {len(unique)} domains in {elapsed:.1f} seconds "
        f"(parallel, limit={limit})"
    )
    return results


def fetch_signals_parallel_sync(
    domains: list[str],
    limit: int = DEFAULT_CONCURRENCY,
) -> dict[str, str]:
    """Sync wrapper around ``fetch_signals_parallel``."""
    return asyncio.run(fetch_signals_parallel(domains, limit=limit))


def domain_from_lead(lead: dict) -> str:
    """Domain from Notes (sheet) or email fallback."""
    notes = (lead.get("Notes") or "").strip()
    if notes and " " not in notes and "." in notes:
        return _normalize_domain(notes)
    email = (lead.get("Email") or "").strip()
    if "@" in email:
        return _normalize_domain(email.split("@", 1)[1])
    return ""


def ensure_signals_for_leads(
    leads: list[dict],
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    write_sheet: bool = True,
) -> dict[str, str]:
    """Fill Signal for leads about to be sent (lazy scrape).

    - Skip if sheet already has Signal
    - Else try local JSON cache
    - Else scrape missing domains in parallel
    - Write Signal back to sheet + cache immediately
    Mutates ``lead["Signal"]`` in place. Returns domain→signal map used.
    """
    from .to_sheets import update_signal

    cache = _load_cache()
    resolved: dict[str, str] = {}
    to_scrape: list[tuple[dict, str]] = []

    for lead in leads:
        existing = (lead.get("Signal") or "").strip()
        domain = domain_from_lead(lead)
        if existing:
            if domain:
                resolved[domain] = existing
                cache[domain] = existing
            continue

        if domain and (cache.get(domain) or "").strip():
            signal = cache[domain].strip()
            lead["Signal"] = signal
            resolved[domain] = signal
            if write_sheet and lead.get("row_index"):
                try:
                    update_signal(int(lead["row_index"]), signal)
                except Exception as e:
                    print(f"WARN: could not write cached signal to sheet: {e}")
            continue

        if domain:
            to_scrape.append((lead, domain))

    if to_scrape:
        scraped = fetch_signals_parallel_sync(
            [d for _, d in to_scrape],
            limit=concurrency,
        )
        for lead, domain in to_scrape:
            signal = (scraped.get(domain) or "").strip()
            lead["Signal"] = signal
            resolved[domain] = signal
            cache[domain] = signal
            if write_sheet and lead.get("row_index"):
                try:
                    update_signal(int(lead["row_index"]), signal)
                except Exception as e:
                    print(f"WARN: could not write signal to sheet row {lead.get('row_index')}: {e}")

    _save_cache(cache)
    return resolved
