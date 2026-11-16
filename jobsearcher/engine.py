"""Run every enabled source concurrently and assemble the tier-sorted feed."""

import re
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import CFG, DEDUP, MAX_AGE_WEEKS, MAX_WORKERS, company_allowed, tier_of
from . import enrich
from .sources import (check_ashby, check_greenhouse, check_lever,
                      check_ycombinator, check_hn_whoishiring, check_remoteok,
                      check_weworkremotely, check_linkedin, check_zohorecruit,
                      check_consider, check_allincapital,
                      _zoho_site_entries, _consider_entries)


def _norm(text) -> str:
    """Whitespace-collapsed, case-folded text for dedup keys."""
    return re.sub(r"\s+", " ", str(text or "")).lower().strip()


def dedup_rows(rows: list) -> tuple:
    """Drop overlapping postings; returns (kept, dropped_count).

    Two keys: the apply URL (catches same posting regardless of how its
    title/fields differ), and (ATS, company, title, location) — which
    collapses identical listings that arrive twice via different links,
    e.g. the heavily overlapping WWR category feeds. A shared title at a
    *different* location survives: those are separate requisitions with
    separate apply links.
    """
    kept, seen_urls, seen_exact = [], set(), set()
    for row in rows:
        url = str(row.get("Direct_Apply_URL") or "").strip().rstrip("/")
        exact = (row["ATS"], _norm(row["Company"]), _norm(row["Title"]),
                 _norm(row["Location"]))
        if url and url in seen_urls:
            continue
        if exact in seen_exact:
            continue
        seen_exact.add(exact)
        if url:
            seen_urls.add(url)
        kept.append(row)
    return kept, len(rows) - len(kept)


def filter_by_age(rows: list, max_age_weeks: int) -> list:
    """Drop postings older than `max_age_weeks`.

    Age is based on the normalized `Posted` field (YYYY-MM-DD) compared to
    today's date at runtime. Rows with no parsable posted date ('n/a') are
    kept: age is unknown, so we prefer keeping them rather than silently
    discarding potentially live postings. Set `max_age_weeks` to 0 or a
    negative value to disable age filtering.
    """
    if not max_age_weeks or max_age_weeks <= 0:
        return rows
    cutoff = date.today() - timedelta(weeks=max_age_weeks)
    kept = []
    for row in rows:
        posted = row.get("Posted")
        if not posted or posted == "n/a":
            # No parsable posted date — keep the row (age unknown).
            kept.append(row)
            continue
        try:
            posted_date = date.fromisoformat(str(posted))
        except ValueError:
            # Unexpected format — prefer keeping over dropping.
            kept.append(row)
            continue
        if posted_date >= cutoff:
            kept.append(row)
    return kept


def collect_jobs(prev_jobs: list | None = None):
    """Fetch every enabled source and return the matching rows, tier-sorted.

    `prev_jobs` (web mode: the last snapshot) seeds the AI enrichment
    layer: rows whose apply URL already appeared there adopt their old
    AI_* annotation instead of costing a second model call. Only survivor
    rows are annotated, and the internal `_desc` key is stripped before
    anything is returned — it must never reach the CSV or the snapshot.
    """
    ats_boards = CFG["boards"]
    all_jobs = []
    tasks = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        if ats_boards["ashby"].get("enabled"):
            for s in ats_boards["ashby"].get("boards", []):
                tasks.append(ex.submit(check_ashby, s))
        if ats_boards["greenhouse"].get("enabled"):
            for s in ats_boards["greenhouse"].get("boards", []):
                tasks.append(ex.submit(check_greenhouse, s))
        if ats_boards["lever"].get("enabled"):
            for s in ats_boards["lever"].get("boards", []):
                tasks.append(ex.submit(check_lever, s))
        for entry in _consider_entries():
            tasks.append(ex.submit(check_consider, entry))
        allin_url = ats_boards.get("allincapital", {}).get("url", "")
        if allin_url:
            tasks.append(ex.submit(check_allincapital, allin_url))
        tasks.append(ex.submit(check_ycombinator))
        tasks.append(ex.submit(check_hn_whoishiring))
        tasks.append(ex.submit(check_remoteok))
        tasks.append(ex.submit(check_linkedin))
        for feed in CFG["feeds"]["weworkremotely"].get("feeds", []):
            tasks.append(ex.submit(check_weworkremotely, feed))
        for site_cfg in _zoho_site_entries():
            tasks.append(ex.submit(check_zohorecruit, site_cfg))
        for fut in as_completed(tasks):
            for row in fut.result():
                if company_allowed(row["Company"]):
                    row["Tier"] = tier_of(row["Location"])
                    all_jobs.append(row)

    if DEDUP:
        all_jobs, _ = dedup_rows(all_jobs)

    all_jobs = filter_by_age(all_jobs, MAX_AGE_WEEKS)

    all_jobs.sort(key=lambda j: (j["Tier"], j["Company"], j["Title"]))

    prev_by_url = {}
    if prev_jobs:
        prev_by_url = {(r.get("Direct_Apply_URL") or "").strip().rstrip("/"): r
                       for r in prev_jobs
                       if (r.get("Direct_Apply_URL") or "").strip()}
    enrich.enrich_jobs(all_jobs, prev_by_url)

    for row in all_jobs:
        row.pop("_desc", None)
    return all_jobs
