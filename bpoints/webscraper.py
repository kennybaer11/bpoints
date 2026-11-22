"""webscraper.io's Cloud Scraper API: the sitemaps bpoints owns, jobs, results.

The same account as posession's football and tennis odds (WEBSCRAPER_TOKEN,
read from posession's .env when bpoints' own has none), so the page credits are
shared: every page a job opens is one credit.

Our sitemaps are defined in code and pushed to the account when missing or
changed; a job's start URLs are set per run, so a saved sitemap never points at
last week's games.
"""

import json
import logging
import os
import time
from pathlib import Path

import requests
from dotenv import dotenv_values

log = logging.getLogger(__name__)

API = "https://api.webscraper.io/api/v1"
POSESSION_ENV = Path(os.getenv("POSESSION_ENV", r"C:\Users\kenny\posession\scraper\.env"))


def token() -> str:
    tok = os.getenv("WEBSCRAPER_TOKEN") or (
        dotenv_values(POSESSION_ENV).get("WEBSCRAPER_TOKEN") if POSESSION_ENV.exists() else None)
    if not tok:
        raise SystemExit(f"WEBSCRAPER_TOKEN is not set (nor in {POSESSION_ENV})")
    return tok


def _req(method: str, path: str, **kw) -> requests.Response:
    kw.setdefault("params", {})["api_token"] = token()
    kw.setdefault("timeout", 60)
    for attempt in range(7):          # through this PC's DNS drops, as elsewhere
        try:
            r = requests.request(method, f"{API}{path}", **kw)
            break
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 6:
                raise
            log.warning("webscraper.io %s: connection failed - retrying", path)
            time.sleep(min(5 * 2 ** attempt, 40))
    r.raise_for_status()
    return r


def credits_left() -> int | None:
    try:
        return _req("GET", "/account").json()["data"]["page_credits"]
    except Exception:
        return None


def ensure_sitemap(sitemap: dict) -> int:
    """The id of our sitemap on the account, created or updated to match `sitemap`."""
    page, found = 1, None
    while found is None:
        body = _req("GET", "/sitemaps", params={"page": page}).json()
        found = next((s for s in body.get("data", []) if s.get("name") == sitemap["_id"]), None)
        if found or page >= (body.get("last_page") or 1):
            break
        page += 1
    text = json.dumps(sitemap, ensure_ascii=False)
    if found is None:
        sid = _req("POST", "/sitemap", data=text.encode("utf-8"),
                   headers={"Content-Type": "application/json"}).json()["data"]["id"]
        log.info("created sitemap %s (%d)", sitemap["_id"], sid)
        return sid
    sid = found["id"]
    stored = _req("GET", f"/sitemap/{sid}").json()["data"].get("sitemap")
    stored = json.loads(stored) if isinstance(stored, str) else stored
    if stored != sitemap:
        _req("PUT", f"/sitemap/{sid}", data=text.encode("utf-8"),
             headers={"Content-Type": "application/json"})
        log.info("updated sitemap %s (%d)", sitemap["_id"], sid)
    return sid


def start_job(sitemap_id: int, start_urls: list[str], request_interval: int = 2000,
              page_load_delay: int = 2500, driver: str = "fulljs") -> int:
    body = {"sitemap_id": sitemap_id, "driver": driver, "page_load_delay": page_load_delay,
            "request_interval": request_interval, "start_urls": list(start_urls)}
    return _req("POST", "/scraping-job", json=body).json()["data"]["id"]


def wait_for(job_id: int, timeout: int = 1800, poll: int = 15) -> dict:
    """Block until a job stops running; its final record."""
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        job = _req("GET", f"/scraping-job/{job_id}").json()["data"]
        if job.get("status") != last:
            last = job.get("status")
            log.info("job %s: %s", job_id, last)
        if last in ("finished", "failed", "stopped"):
            return job
        time.sleep(poll)
    raise TimeoutError(f"job {job_id} still running after {timeout}s")


def job_rows(job_id: int) -> list[dict]:
    """A job's scraped records (the endpoint answers JSON Lines)."""
    out = []
    for line in _req("GET", f"/scraping-job/{job_id}/json").text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out
