"""Scrape a site signal for personalization (openers are written by the agent)."""

import time

import httpx
from bs4 import BeautifulSoup


def fetch_signal(url: str) -> str:
    """Fetch title / h1 / meta description from a website."""
    try:
        r = httpx.get(url, follow_redirects=True, timeout=10)
        s = BeautifulSoup(r.text, "html.parser")
        title = s.title.get_text(strip=True) if s.title else ""
        h1 = s.h1.get_text(strip=True) if s.h1 else ""
        meta = s.find("meta", attrs={"name": "description"})
        desc = meta["content"].strip() if meta and meta.get("content") else ""
        return f"Page: {title} | H1: {h1} | Meta: {desc}"
    except Exception:
        return ""
    finally:
        time.sleep(1)  # be polite between website fetches


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
