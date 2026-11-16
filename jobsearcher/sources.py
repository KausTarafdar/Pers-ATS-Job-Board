"""Source checkers: one function per job board or feed.

Every `check_*` fetches one public endpoint and returns a list of
normalized rows (see `_row`). Failures are swallowed (`except Exception`),
so a single dead board or rate-limited feed never kills a run —
`jobsearcher.cli.verify_boards` is the loud path for finding dead slugs.

Company/location field shapes differ per ATS: Greenhouse nests location
under `location.name` and must be fetched with `?content=true` or the
description (and therefore the YOE filter) silently disappears; Lever
locations sit under `categories.location` with `workplaceType`, and its
descriptions come in the plain `?mode=json` payload; Ashby has a separate
`isRemote` flag and takes an `/application` suffix on its apply links.
"""

import email.utils
import gzip
import http.cookiejar
import html
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree
from datetime import datetime, timezone

from .fetch import fetch_json, fetch_text
from .config import (CFG, RESUME_PDF, TIMEOUT, ROLE_REGEX, EXCLUDE_ROLE_REGEX,
                     LOCATION_REGEX, EXCLUDE_LOC_REGEX, MAX_YOE,
                     title_ok, location_ok, yoe_ok,
                     stated_max_years, description_text)


def _iso_date(raw) -> str | None:
    """Normalize a source-provided posted timestamp to 'YYYY-MM-DD' or None.

    Accepts epoch seconds/milliseconds (int or digit-string) and ISO or
    RFC-2822 datetime strings — the shapes the ATSes and feeds actually send
    (Lever/Ashby/RemoteOK epoch, Greenhouse ISO, WWR RFC-2822). Anything we
    can't parse just yields None and the row shows n/a."""
    try:
        if isinstance(raw, (int, float)):
            ts = raw
        elif isinstance(raw, str) and re.fullmatch(r"-?\d{9,16}", raw.strip()):
            ts = int(raw.strip())
        elif isinstance(raw, str) and raw.strip():
            raw = raw.strip()
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            ts = None
            try:
                dt = datetime.fromisoformat(raw)
            except ValueError:
                dt = email.utils.parsedate_to_datetime(raw)   # RFC-2822 (RSS)
        else:
            return None
        if ts is not None:
            dt = datetime.fromtimestamp(ts / 1000 if ts > 1e11 else ts,
                                        tz=timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return None


def _row(ats: str, company: str, title: str, loc: str, stated_yoe, url: str,
         desc: str = "", posted=None) -> dict:
    """Normalized row shape shared by every source.

    `desc` is internal plumbing for the optional AI annotation layer
    (engine passes it to enrich, then strips the key before any row
    escapes into the CSV/snapshot). An empty/absent desc — YC and
    LinkedIn guest cards — simply leaves the key off the row. `posted` is
    the source's best-effort posting timestamp; rows without one show a
    'n/a' date on the dashboard and sort last."""
    posted_date = _iso_date(posted)
    row = {
        "ATS": ats,
        "Company": company,
        "Title": title,
        "Location": loc,
        "Max_Stated_YOE": stated_yoe if stated_yoe is not None else "n/a",
        "Posted": posted_date if posted_date else "n/a",
        "Recommended_PDF": RESUME_PDF,
        "Direct_Apply_URL": url,
    }
    if desc:
        row["_desc"] = desc
    return row


def check_ashby(slug: str):
    results = []
    try:
        data = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        for j in data.get("jobs", []):
            title = j.get("title", "")
            loc = j.get("location", "") or ""
            is_remote = j.get("isRemote", False)
            loc_str = f"{loc} (Remote)" if is_remote and "remote" not in loc.lower() else loc
            if not title_ok(title) or not location_ok(loc_str):
                continue
            desc = description_text(j.get("descriptionPlain") or "",
                                    j.get("descriptionHtml") or "")
            stated_yoe = stated_max_years(desc)
            if not yoe_ok(desc):
                continue
            url = j.get("jobUrl") or f"https://jobs.ashbyhq.com/{slug}/{j.get('id')}"
            results.append(_row("Ashby", slug.title(), title, loc_str, stated_yoe,
                                url.rstrip("/") + "/application", desc,
                                posted=j.get("publishedAt") or j.get("updatedAt")))
    except Exception:
        pass
    return results


def check_greenhouse(slug: str):
    results = []
    try:
        # ?content=true is required or the description is absent and the YOE
        # filter silently stops applying.
        data = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
        for j in data.get("jobs", []):
            title = j.get("title", "")
            loc = (j.get("location") or {}).get("name", "")
            if not title_ok(title) or not location_ok(loc):
                continue
            desc = description_text(j.get("content") or "")
            stated_yoe = stated_max_years(desc)
            if not yoe_ok(desc):
                continue
            url = j.get("absolute_url", "")
            results.append(_row("Greenhouse", slug.title(), title, loc, stated_yoe, url,
                                desc, posted=j.get("first_published") or j.get("updated_at")))
    except Exception:
        pass
    return results


def check_lever(slug: str):
    results = []
    try:
        data = fetch_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
        if not isinstance(data, list):
            return results
        for j in data:
            title = j.get("text", "")
            cats = j.get("categories") or {}
            loc = cats.get("location", "") or ""
            if not loc and isinstance(cats.get("allLocations"), list) and cats["allLocations"]:
                loc = cats["allLocations"][0]
            workplace = j.get("workplaceType", "")
            loc_str = f"{loc} ({workplace})" if workplace else str(loc)
            if not title_ok(title) or not location_ok(loc_str):
                continue
            list_texts = [li.get("content", "") for li in (j.get("lists") or [])]
            desc = description_text(j.get("descriptionPlain") or "", *list_texts)
            stated_yoe = stated_max_years(desc)
            if not yoe_ok(desc):
                continue
            url = j.get("applyUrl") or j.get("hostedUrl", "")
            results.append(_row("Lever", slug.title(), title, loc_str, stated_yoe, url,
                                desc, posted=j.get("createdAt")))
    except Exception:
        pass
    return results


# ---------------- Non-ATS sources (all fail silently) ----------------

def check_ycombinator():
    """One public guest fetch of YC Work at a Startup eng listings.

    Guest access serves a fixed curated sample (~30 recently-active eng
    jobs) of the full 2900+ listing board; no description text is exposed,
    so the YOE filter cannot apply here (rows show n/a). Filters and
    pagination on the page are client-side Inertia and are ignored."""
    if not CFG["feeds"]["yc"].get("enabled"):
        return []
    results = []
    try:
        page = fetch_text("https://www.workatastartup.com/jobs?roles=eng")
        blob = re.search(r'data-page="([^"]+)"', page)
        if not blob:
            return results
        props = json.loads(html.unescape(blob.group(1))).get("props", {})
    except Exception:
        return results
    for j in props.get("jobs", []):
        title = j.get("title", "")
        loc = j.get("location", "") or ""
        if not title_ok(title) or not location_ok(loc):
            continue
        slug = j.get("companySlug") or ""
        if slug and j.get("id"):
            url = f"https://www.workatastartup.com/companies/{slug}/jobs/{j['id']}"
        else:
            url = j.get("applyUrl", "")
        results.append(_row("YC", j.get("companyName") or "YC startup", title,
                            loc, None, url))
    return results


# Region keywords used by the HN "Remote (US)" window check. Kept here rather
# than in config because it only matters for free-form HN posts; tuned for an
# India timezone — SE Asia counts as reachable, Europe/Americas do not, so a
# US-remote post naming an out-of-region city nearby stays excluded.
_HN_ALLOWED_REGIONS = re.compile(
    r"\b(india|bengaluru|bangalore|gurgaon|gurugram|apac|singapore|bangkok|"
    r"thailand|indonesia|jakarta|malaysia|kuala lumpur|vietnam|philippines|"
    r"manila|southeast asia|global|worldwide)\b", re.I)


def check_hn_whoishiring():
    """Monthly 'Ask HN: Who is hiring?' thread via the public Algolia API.

    Seed-stage heavy. Company/title come from the post's opening line and
    the YOE filter runs on the full post text."""
    if not CFG["feeds"]["hn"].get("enabled"):
        return []
    results = []
    try:
        stories = fetch_json(
            f"https://hn.algolia.com/api/v1/search_by_date"
            f"?query=%22Who%20is%20hiring%22&tags=story,author_whoishiring&hitsPerPage=10"
        )
        hits = stories.get("hits", [])
        if not hits:
            return results
        latest = max(hits, key=lambda h: h.get("created_at", ""))
        thread = fetch_json(
            f"https://hn.algolia.com/api/v1/search_by_date"
            f"?tags=comment,story_{latest['objectID']}&hitsPerPage=1000"
        )
    except Exception:
        return results
    for c in thread.get("hits", []):
        raw = c.get("comment_text") or ""
        try:
            first_link = html.unescape(re.search(r'<a\s+href="([^"]+)"', raw).group(1))
            first_link = re.sub(r"[\),\.]+$", "", first_link)
        except AttributeError:
            first_link = None
        text = re.sub(r"\s+", " ", description_text(raw)).strip()
        if not text or not ROLE_REGEX.search(text):
            continue
        # Structured posts look like "Company | Role | Location"; prefer that,
        # falling back to the opening 150 chars for freer-form posts. Skip
        # segments that are bare links, "Remote …", or a salary figure — some
        # posts put those before the actual role name.
        parts = [p.strip() for p in text.split(" | ")]
        candidate = next((p for p in parts[1:4]
                          if not re.match(r"^https?://", p)
                          and not re.match(r"^remote\b", p, re.I)
                          and "$" not in p[:24]), None)
        if len(parts) >= 2 and candidate:
            title = candidate
            exclude_scope = candidate + " " + " ".join(parts[3:5])[:120]
        else:
            title = parts[0]
            exclude_scope = text[:150]
        if EXCLUDE_ROLE_REGEX.search(exclude_scope):
            continue
        # Forget links (and parenthetical URLs) when naming the company, and
        # prefer the segment before " - " (posts like "DuckDuckGo - we are
        # looking for candidates that ..." name the company first).
        company = re.split(r"\bhttps?:", parts[0])[0].split("(")[0].strip()
        company = re.split(r"\s+[-–—]\s+", company)[0]
        company = re.sub(r"^\W+|\W+$", "", company)
        company = company[:60] if len(company) > 3 or not company else "HN poster"
        # Some posters open with plain metadata ("Location: X Remote: Yes …",
        # "Position: …") instead of a company name; no usable Company/Title
        # can be parsed from those, so drop the post outright.
        if re.match(r"^(?:location|remote|position|title|salary|apply|experience)\s*:",
                    company, re.I) \
           or re.match(r"^(?:location|remote|position|title|salary|apply|experience)\s*:",
                       title, re.I):
            continue
        lm = LOCATION_REGEX.search(text)
        if not lm or EXCLUDE_LOC_REGEX.search(text):
            continue
        # "Remote" posts that are remote-US-only: check a window around the
        # location mention for US/Canada markers, unless an explicitly allowed
        # country/region is named in the same window (e.g. "North America AND
        # Europe", "US OR EU" — those stay).
        window = text[max(0, lm.start() - 80):lm.end() + 80]
        if re.search(r"\b(u\.?s\.?\.?|usa|united states|north america|canada)\b", window, re.I) \
           and not _HN_ALLOWED_REGIONS.search(window):
            continue
        stated_yoe = stated_max_years(text)
        if not yoe_ok(text):
            continue
        results.append(_row("HN", company[:60] or "HN poster", title[:150],
                            lm.group(0), stated_yoe,
                            first_link or f"https://news.ycombinator.com/item?id={c.get('objectID')}",
                            text, posted=c.get("created_at")))
    return results


def check_remoteok():
    """https://remoteok.com/api — recent 100 remote postings with descriptions."""
    if not CFG["feeds"]["remoteok"].get("enabled"):
        return []
    results = []
    try:
        data = fetch_json("https://remoteok.com/api")
    except Exception:
        return results
    for j in data:
        if not isinstance(j, dict) or not j.get("position"):
            continue  # first element is the API's legal meta blob
        title = j.get("position", "")
        if not title_ok(title):
            continue
        loc = j.get("location") or ""
        if not loc.strip() or LOCATION_REGEX.search(loc) is None:
            if (loc or "").strip().lower() not in ("worldwide", "remote", "anywhere", "earth", ""):
                continue
            loc = "Remote (Global)"
        if EXCLUDE_LOC_REGEX.search(loc):
            continue
        desc = description_text(j.get("description") or "")
        stated_yoe = stated_max_years(desc)
        if not yoe_ok(desc):
            continue
        results.append(_row("RemoteOK", (j.get("company") or "RemoteOK company")[:60],
                            title[:150], loc, stated_yoe,
                            j.get("apply_url") or j.get("url", ""), desc,
                            posted=j.get("date") or j.get("epoch")))
    return results


def check_weworkremotely(feed: str):
    """WeWorkRemotely category RSS (full-stack & general programming)."""
    if not CFG["feeds"]["weworkremotely"].get("enabled"):
        return []
    results = []
    try:
        raw = fetch_text(f"https://weworkremotely.com/categories/{feed}.rss")
        root = xml.etree.ElementTree.fromstring(raw)
    except Exception:
        return results
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = item.findtext("pubDate") or ""
        if not title_ok(title):
            continue
        lm = LOCATION_REGEX.search(title)
        # WWR items are all remote; the parenthetical usually narrows region.
        loc = lm.group(0) if lm else "Remote (Global)"
        if EXCLUDE_LOC_REGEX.search(title):
            continue
        desc = description_text(item.findtext("description") or "")
        stated_yoe = stated_max_years(desc)
        if not yoe_ok(desc):
            continue
        results.append(_row("WWR", re.split(r":\s*", title)[0][:60], title[:150],
                            loc, stated_yoe, link, desc, posted=pub))
    return results


def check_linkedin():
    """LinkedIn guest search cards. Rate-limited: expect 429s some runs.

    No description without a second request per job, so the YOE filter
    cannot apply and Max_Stated_YOE shows n/a for these rows. One request
    per query — per-job detail fetches would trip the limiter, so never
    add them."""
    if not CFG["feeds"]["linkedin"].get("enabled"):
        return []
    results = []
    for query_url in CFG["feeds"]["linkedin"].get("queries", []):
        try:
            frag = fetch_text(query_url)
        except Exception:
            continue  # 429/999 — skip this query quietly
        # Each card: <div class="base-card ... data-entity-urn="urn:li:jobPosting:<id>">
        for card in re.split(r'<div\s+class="base-card[^"]*"', frag)[1:]:
            jid = re.search(r'data-entity-urn="urn:li:jobPosting:(\d+)"', card)
            t = re.search(r'base-search-card__title[^>]*>\s*([^<]+?)\s*<', card)
            # company name sits in the subtitle (often wrapped in an <a>)
            s = re.search(r'base-search-card__subtitle[^>]*>\s*(?:<a[^>]*>)?\s*([^<]+?)\s*'
                          r'(?:</a>)?\s*<', card)
            l = re.search(r'job-search-card__location[^>]*>\s*([^<]+?)\s*<', card)
            dt = re.search(r'job-search-card__list-date[^>]*\bdatetime="([^"]+)"', card)
            if not (jid and t):
                continue
            company = html.unescape(s.group(1)).strip() if s else "LinkedIn posting"
            title = html.unescape(t.group(1)).strip()
            loc = html.unescape(l.group(1)).strip() if l else ""
            if not title_ok(title) or not location_ok(loc):
                continue
            results.append(_row("LinkedIn", company[:60], title[:150], loc, None,
                                f"https://www.linkedin.com/jobs/view/{jid.group(1)}",
                                posted=dt.group(1) if dt else None))
    return results


def _zoho_site_entries():
    """Parse feeds.zoho.sites entries of the form "company | site | page_name"."""
    out = []
    for raw in CFG["feeds"]["zoho"].get("sites", []):
        parts = [p.strip() for p in str(raw).split("|")]
        if len(parts) != 3 or not all(parts):
            raise ValueError(
                f"config feeds.zoho.sites entry {raw!r} must be 'company | site | page_name'")
        out.append({"company": parts[0], "site": parts[1], "page_name": parts[2]})
    return out


# ---------------- Portfolio boards (Consider platform + embedded JSON) ----

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def _consider_entries():
    """Parse boards.consider entries of the form "board_id | host"."""
    out = []
    for raw in CFG["boards"].get("consider", {}).get("boards", []):
        parts = [p.strip() for p in str(raw).split("|")]
        if len(parts) != 2 or not all(parts):
            raise ValueError(
                f"config boards.consider entry {raw!r} must be 'board_id | host'")
        out.append({"board_id": parts[0], "host": parts[1]})
    return out


def _consider_search_all(opener, host: str, board_id: str) -> list:
    """Page through a Consider board's POST search-jobs API.

    The board pages (jobs.lsvp.com etc.) fetch their listings with a
    POST /api-boards/search-jobs call guarded by a session cookie and a
    CSRF token embedded in the page HTML; results page via a `sequence`
    cursor inside `meta`. The full portfolio boards are enormous (Lightspeed
    alone lists thousands of mostly-GTM postings), so the search is scoped
    to the board's engineering-ish `departments` facet values (the board's
    own autocomplete endpoint provides them); an empty result there falls
    back to an unscoped search. Returns the raw job dicts."""
    # The CSRF token only serves together with the session cookie the page
    # sets, and the page occasionally 412s under bursts — a small retry loop.
    token = None
    for attempt in range(3):
        try:
            page = opener.open(urllib.request.Request(
                f"https://{host}/jobs", headers={"User-Agent": _UA}),
                timeout=TIMEOUT).read().decode("utf-8", errors="ignore")
            m = re.search(r'"csrfToken":"([^"]+)', page)
            if m:
                token = m.group(1)
                break
        except Exception:
            time.sleep(2 * (attempt + 1))
    if not token:
        return []

    def post(path: str, payload: dict):
        req = urllib.request.Request(
            f"https://{host}{path}",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"User-Agent": _UA,
                     "Content-Type": "application/json",
                     "Accept": "application/json",
                     "Accept-Encoding": "gzip",
                     "CSRF-Token": token})
        resp = opener.open(req, timeout=TIMEOUT)
        raw = resp.read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8", errors="ignore"))

    # Scope to engineering departments via the board's facet values — an
    # unscoped search on the big portfolio boards pages through tens of
    # thousands of GTM postings (minutes) for the same final rows.
    departments = ["Engineering"]
    try:
        facets = post("/api-boards/autocomplete/departments",
                      {"board": board, "q": ""})
        vals = [f.get("self", {}).get("value", "")
                for f in facets.get("results", [])]
        eng = [v for v in vals
               if v and re.search(r"engineer|software|developer|technical", v, re.I)]
        if eng:
            departments = eng
    except Exception:
        pass  # unscoped fallback below
    jobs, sequence = [], None
    board = {"id": board_id, "isParent": True}
    for _ in range(2000):  # hard safety cap against runaway pagination
        payload = {"meta": {"size": 100},
                   "board": board,
                   "query": {"departments": departments},
                   "grouped": False}
        if sequence:
            payload["meta"]["sequence"] = sequence
        try:
            data = post("/api-boards/search-jobs", payload)
        except Exception:
            break
        batch = data.get("jobs", [])
        jobs.extend(batch)
        sequence = (data.get("meta") or {}).get("sequence")
        if not sequence or not batch:
            break
    return jobs


def check_consider(entry: dict):
    """VC portfolio boards hosted on Consider (lightspeed/chiratae/peak xv).

    No JD text is exposed, so the strict YOE filter cannot read the
    description — but the API ships a computed `minYearsExp` field, which
    is gated against the config cap instead. Rows without it show n/a."""
    if not CFG["boards"].get("consider", {}).get("enabled"):
        return []
    results = []
    try:
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        jobs = _consider_search_all(opener, entry["host"], entry["board_id"])
    except Exception:
        return results
    for j in jobs:
        title = j.get("title", "")
        if not title_ok(title):
            continue
        # minYearsExp is Consider's own parsed minimum — same strict stance:
        # above the cap is dropped, absent means unknown.
        stated_yoe = j.get("minYearsExp")
        try:
            stated_yoe = int(stated_yoe) if stated_yoe is not None else None
        except (TypeError, ValueError):
            stated_yoe = None
        if stated_yoe is not None and stated_yoe > MAX_YOE:
            continue
        locs = [str(l) for l in (j.get("locations") or []) if l]
        if j.get("remote"):
            base = next((l for l in locs if LOCATION_REGEX.search(l)),
                        locs[0] if locs else "")
            loc = (f"{base} (Remote)"
                   if base and "remote" not in base.lower()
                   else (base or "Remote (Global)"))
        else:
            loc = locs[0] if locs else "Remote (Global)"
        if not location_ok(loc):
            continue
        url = j.get("applyUrl") or j.get("url") or ""
        results.append(_row("Consider", (j.get("companyName") or "portfolio")[:60],
                            title[:150], loc, stated_yoe, url,
                            posted=j.get("timeStamp")))
    return results


def check_allincapital(url: str):
    """All In Capital's founders board embeds its full job list as escaped
    JSON inside the Next.js page payload. Walk the escaped key/value pairs
    in order and group them into job objects (each object starts at
    `id` followed by `company_id`). Descriptions are RSC references there,
    so no JD text — YOE shows n/a."""
    if not CFG["boards"].get("allincapital", {}).get("enabled"):
        return []
    try:
        page = fetch_text(url)
    except Exception:
        return []
    results = []
    # The Next.js payload's escape depth varies between responses (\" vs
    # \\"), so normalize every backslash run before a quote to one.
    page = re.sub(r'\\+(")', r"\1", page)
    tokens = re.finditer(r'"([a-z_]+)":"((?:[^"\\]|\\.)*)"', page)
    jobs, current, prev_name = [], None, ""
    for m in tokens:
        name, raw = m.group(1), m.group(2)
        if name == "company_id" and prev_name == "id":
            if current:
                jobs.append(current)
            current = {}
        if current is not None:
            try:
                current[name] = json.loads(f'"{raw}"')
            except Exception:
                current[name] = raw
        prev_name = name
    if current:
        jobs.append(current)
    for j in jobs:
        if j.get("status") != "live":
            continue
        title = j.get("title") or ""
        if not title_ok(title):
            continue
        loc = j.get("location") or ""
        if not location_ok(loc):
            continue
        company = (j.get("company_name") or "All In portfolio")[:60]
        url_job = j.get("apply_url") or j.get("source_url") or ""
        results.append(_row("AllIn", company, title[:150], loc, None,
                            url_job, posted=j.get("published_at")))
    return results


def check_zohorecruit(site_cfg: dict):
    """Zoho Recruit lister via the public endpoint embed_jobs.js queries."""
    if not CFG["feeds"]["zoho"].get("enabled"):
        return []
    results = []
    company = site_cfg["company"]
    site = site_cfg["site"].rstrip("/")
    try:
        data = fetch_json(
            f"{site}/recruit/v2/public/Job_Openings"
            f"?pagename={urllib.parse.quote(site_cfg['page_name'])}"
        )
    except Exception:
        return results
    if not isinstance(data, dict) or data.get("code") != "success":
        return results
    for j in data.get("data", []):
        title = (j.get("Posting_Title") or j.get("Job_Opening_Name") or "").strip()
        loc = ", ".join(x for x in (j.get("City"), j.get("State"), j.get("Country")) if x)
        if not title_ok(title):
            continue
        if not LOCATION_REGEX.search(loc or "remote") and not LOCATION_REGEX.search(title):
            continue
        if EXCLUDE_LOC_REGEX.search(loc):
            continue
        desc = description_text(j.get("Job_Description") or "")
        stated_yoe = stated_max_years(desc)
        if not yoe_ok(desc):
            continue
        results.append(_row("Zoho", company, title[:150], loc or "Remote (Global)",
                            stated_yoe, j.get("$url", ""), desc))
    return results
