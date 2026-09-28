#!/usr/bin/env python3
"""
watcher.py — "Econ Job Finder" dashboard for Marcelo.

Writes dashboard.html (self-contained: data baked in, open in any browser) and emails it
as an attachment with a short note listing the NEW roles:
  Roles tab   — open roles auto-pulled from employer ATS feeds, careers pages, aggregators
                and email alerts, CLASSIFIED into a job type (Central banks / Policy-IO /
                Think tanks / Private sector → Consulting / Academic → sub-types), with
                location mapped to region → country and deadlines parsed. Filter by type,
                location, deadline, text, NEW.
  Sources tab — every sources.yaml entry with its Source Tracker status (`status:` in the yaml):
                directly / alert = tracking, blocked / ignored = not tracking.
JOE/EJM excluded on purpose (Marcelo checks those manually).

Usage: python watcher.py [--init | --dry-run]   (--dry-run writes the dashboard, sends nothing)
Gmail creds in credentials.local [email] address/app_password/to.
Add employers to WORKDAY / SMARTRECRUITERS. ponytail: stdlib + curl (Windows-bundled).
"""
import re, sqlite3, sys, argparse, ssl, html, json, smtplib, subprocess, urllib.request, urllib.parse, imaplib, email as emailmod, base64, unicodedata
from collections import Counter
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from datetime import datetime, timedelta, date

BASE  = Path(__file__).resolve().parent
SRCS  = BASE / "sources.yaml"
CREDS = BASE / "credentials.local"
DB    = BASE / "seen.db"
UA    = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CTX   = ssl.create_default_context()

GERMAN_REQ = re.compile(r"german\s+(language\s+)?(is\s+)?(required|mandatory|essential)"
                        r"|fluent\s+in\s+german|verhandlungssicher|deutsch\s+(ist\s+)?(erforderlich|zwingend|voraussetzung)", re.I)
ROLE_RE = re.compile(r"econom|research|policy analyst|policy officer|fellow|quantitative|statistic|lecturer|professor|post-?doc|economist", re.I)
# Roles to drop: too senior (assoc/full prof), too junior (PhD/doctoral student), or internships
ACAD_EXCLUDE = re.compile(r"associate professor|full professor|\bph\.?d\b|\bdoctoral\b|pre-?doctoral|predoctoral", re.I)
INTERN_EXCLUDE = re.compile(r"\binterns?\b|\binternships?\b|\broster\b|\btalent\s*pool\b", re.I)

# ── employer ATS feeds (clean JSON) ─────────────────────────────────────────
WORKDAY = [
    ("IMF",              "imf.wd5.myworkdayjobs.com",   "imf", "IMF"),
    ("Banque de France", "bdf.wd103.myworkdayjobs.com", "bdf", "recrutement-banque-de-France"),
    ("WTO",              "wto.wd103.myworkdayjobs.com", "wto", "External"),
    ("Danmarks NB",      "nationalbanken.wd103.myworkdayjobs.com", "nationalbanken", "Danmarks_Nationalbank"),
    ("Compass Lexecon",  "fticonsulting.wd108.myworkdayjobs.com", "fticonsulting", "CompassLexeconCareers"),
    ("Cornerstone Research", "cornerstone.wd501.myworkdayjobs.com", "cornerstone", "CornerstoneResearch_Careers"),
    ("Lombard Odier",    "lombardodier.wd3.myworkdayjobs.com", "lombardodier", "Lombard_Odier_Careers"),
    ("WFP",              "wfp.wd3.myworkdayjobs.com",         "wfp",          "job_openings"),
]
SMARTRECRUITERS = [("OECD", "OECD")]
WD_QUERIES = ["economist", "research", "economics"]

# ── aggregator search URLs (already economics-filtered) ─────────────────────
UNJOBS_SEARCHES = ["economist", "economics", "trade", "climate", "development"]
UNJOBS_CAP = 50
IMPACTPOOL_SEARCHES = ["economist", "economics", "economic affairs"]
JOBSACUK_URLS = ["https://www.jobs.ac.uk/search/economics?sortOrder=1&pageSize=50"]
INOMICS_URLS = ["https://inomics.com/search?job=job-junior-industry%2Cjob-mid-level-industry%2Cjob-other%2Cjob-postdoc%2Cjob-practicioner%2Cjob-professor%2Cjob-researcher%2Cjob-senior-industry%2Cjob-senior-reasercher"]
EURAXESS_URLS = ["https://euraxess.ec.europa.eu/jobs/search?f%5B0%5D=job_research_field%3A117&f%5B1%5D=job_research_field%3A120&f%5B2%5D=job_research_field%3A121&f%5B3%5D=job_research_field%3A122&f%5B4%5D=job_research_field%3A128&f%5B5%5D=job_research_field%3A131&f%5B6%5D=job_research_field%3A132&f%5B7%5D=job_research_field%3A137&f%5B8%5D=job_research_field%3A140&f%5B9%5D=job_research_field%3A149&f%5B10%5D=job_research_field%3A157&f%5B11%5D=job_research_field%3A212&f%5B12%5D=job_research_field%3A229&f%5B13%5D=job_research_field%3A311&f%5B14%5D=job_research_field%3A368&f%5B15%5D=job_research_field%3A369&f%5B16%5D=job_research_field%3A371&f%5B17%5D=job_research_field%3A373&f%5B18%5D=job_research_field%3A395&f%5B19%5D=positions%3Aother_positions&f%5B20%5D=positions%3Apostdoc_positions"]


def http_get(url, timeout=45):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en"})
    return urllib.request.urlopen(req, timeout=timeout, context=CTX).read().decode("utf-8", "replace")


def http_post_json(url, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30, context=CTX).read().decode("utf-8", "replace"))


def curl_get(url, timeout=30):
    # ponytail: several aggregators WAF-block urllib but allow curl (ships with Windows 10+).
    try:
        r = subprocess.run(["curl", "-s", "-A", UA, "--max-time", str(timeout), url],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout + 10)
        return r.stdout or ""
    except Exception:
        return ""

def urllib_get(url, timeout=15):
    # Some sites (ADB) block curl but allow urllib. Fallback fetcher.
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        return urllib.request.urlopen(req, context=CTX, timeout=timeout).read().decode(errors="replace")
    except Exception:
        return ""


def _anchors(h, pattern):
    for m in re.finditer(pattern, h, re.S):
        href = m.group(1)
        txt = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2)))).strip()
        if txt:
            yield txt, href


# ── adapters: yield {source, org, ext_id, title, loc, deadline, url, scan} ───
WD_TIGHT = {"WFP"}  # large UN orgs: only match "econom" in title, not generic ROLE_RE

def adapter_workday():
    for label, host, tenant, site in WORKDAY:
        api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
        seen = set()
        for q in WD_QUERIES:
            try:
                res = http_post_json(api, {"limit": 20, "offset": 0, "searchText": q})
            except Exception:
                continue
            for p in res.get("jobPostings", []):
                path = p.get("externalPath", "") or p.get("title", "")
                title = (p.get("title") or "").strip()
                if path in seen:
                    continue
                if label in WD_TIGHT:
                    if not re.search(r"econom", title, re.I):
                        continue
                elif not ROLE_RE.search(title):
                    continue
                seen.add(path)
                blurb = " ".join(p.get("bulletFields") or [])
                dl = re.search(r"\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}", blurb)
                yield {"source": label, "org": label, "ext_id": path, "title": title,
                       "loc": (p.get("locationsText") or "").strip(),
                       "deadline": dl.group(0) if dl else "",
                       "url": f"https://{host}/{site}{p.get('externalPath', '')}",
                       "scan": f"{title} {blurb}".lower()}


def adapter_smartrecruiters():
    for label, comp in SMARTRECRUITERS:
        try:
            data = json.loads(http_get(f"https://api.smartrecruiters.com/v1/companies/{comp}/postings?limit=100"))
        except Exception:
            continue
        for p in data.get("content", []):
            title = (p.get("name") or "").strip()
            if not ROLE_RE.search(title):
                continue
            loc = p.get("location") or {}
            pid = p.get("id", "")
            yield {"source": label, "org": label, "ext_id": pid, "title": title,
                   "loc": loc.get("fullLocation") or loc.get("city") or "", "deadline": "",
                   "url": f"https://jobs.smartrecruiters.com/{comp}/{pid}", "scan": title.lower()}


MAXPAGES = 8   # paginate aggregators up to this many pages; stop early when a page adds nothing new


def _paginate(source, page_url, pattern, base, id_fn, loc_fn, title_group=2):
    seen = set()
    for n in range(1, MAXPAGES + 1):
        h = curl_get(page_url(n))
        if not h:
            break
        new_links = 0
        for m in re.finditer(pattern, h, re.S):
            href = m.group(1)
            jid = id_fn(href)
            if not jid or jid in seen:
                continue
            seen.add(jid); new_links += 1     # count all new listings, so econ-sparse sites still paginate
            title = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(title_group)))).strip()
            if not title or not ROLE_RE.search(title):
                continue
            yield {"source": source, "org": source, "ext_id": jid, "title": title[:130],
                   "loc": loc_fn(href), "deadline": "", "url": base + href, "scan": title.lower()}
        if new_links == 0:        # page added no new listings → real end of results
            break


def adapter_unjobs():
    seen = set(); count = 0
    block_rx = re.compile(r'<div[^>]*class="job">(.*?)</div>', re.S)
    link_rx = re.compile(r'class="jtitle"[^>]*href="(https://unjobs\.org/vacancies/(\d+))"[^>]*>([^<]{6,170})</a>')
    org_rx = re.compile(r'<br>\s*([^<]{5,120})\s*<br>\s*Updated:', re.S)
    UN_ROLE = re.compile(r"econom|economic affairs|policy analyst|trade analyst", re.I)
    for term in UNJOBS_SEARCHES:
        if count >= UNJOBS_CAP:
            break
        for pg in range(1, 4):
            h = curl_get(f"https://unjobs.org/search/{term}" + ("" if pg == 1 else f"/{pg}"))
            hit = 0
            for bm in block_rx.finditer(h):
                block = bm.group(1)
                m = link_rx.search(block)
                if not m:
                    continue
                url, vid, raw_title = m.group(1), m.group(2), html.unescape(m.group(3)).strip()
                if vid in seen or not UN_ROLE.search(raw_title):
                    continue
                seen.add(vid); count += 1; hit += 1
                # title often contains "TITLE, City" or "TITLE, City, Country"
                parts = raw_title.rsplit(",", 1)
                if len(parts) == 2 and len(parts[1].strip()) < 50:
                    title, loc = parts[0].strip(), parts[1].strip()
                else:
                    title, loc = raw_title, ""
                om = org_rx.search(block)
                org = html.unescape(om.group(1).strip()) if om else "UNjobs (UN system)"
                yield {"source": "UNjobs", "org": org, "ext_id": vid, "title": title,
                       "loc": loc, "deadline": "", "url": url, "scan": raw_title.lower()}
                if count >= UNJOBS_CAP:
                    break
            if hit == 0 or count >= UNJOBS_CAP:
                break


def adapter_jobsacuk():
    yield from _paginate("jobs.ac.uk",
        lambda n: f"https://www.jobs.ac.uk/search/economics?sortOrder=1&pageSize=50&startIndex={(n - 1) * 50 + 1}",
        r'<a[^>]*href="(/job/[A-Za-z0-9]+/[^"]+)"[^>]*>(.*?)</a>',
        "https://www.jobs.ac.uk",
        lambda h: h.split("/")[2] if len(h.split("/")) > 2 else "",
        lambda h: "UK")


def _jsonld_jobposting(h):
    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', h, re.S):
        try:
            d = json.loads(m.group(1))
        except Exception:
            continue
        for o in (d if isinstance(d, list) else [d]):
            if isinstance(o, dict) and o.get("@type") == "JobPosting":
                return o
    return {}


def adapter_inomics(cap=40):
    # List rows only give a slug; fetch each econ job's detail for JSON-LD
    # (clean title + employer + location + deadline). Enrich up to `cap` per run.
    base = INOMICS_URLS[0]; seen = set(); yielded = 0
    for n in range(1, MAXPAGES + 1):
        h = curl_get(base + f"&page={n}")
        if not h:
            break
        new_links = 0
        for href in re.findall(r'href="(/job/[^"]+)"', h):
            mid = re.search(r"-(\d+)$", href)
            jid = mid.group(1) if mid else href
            if jid in seen:
                continue
            seen.add(jid); new_links += 1
            slug_title = re.sub(r"-\d+$", "", href.split("/job/")[1]).replace("-", " ").strip().title()
            if not ROLE_RE.search(slug_title) or yielded >= cap:
                continue
            d = _jsonld_jobposting(curl_get("https://inomics.com" + href))
            title = (d.get("title") or slug_title).strip()
            org = ((d.get("hiringOrganization") or {}).get("name") or "Inomics").strip()
            jl = d.get("jobLocation") or {}
            loc = (jl.get("name") or (jl.get("address") or {}).get("addressLocality") or "") if isinstance(jl, dict) else ""
            dl = (d.get("validThrough") or "")[:10]
            yielded += 1
            yield {"source": "Inomics", "org": org, "ext_id": jid, "title": title[:130],
                   "loc": loc, "deadline": dl, "url": "https://inomics.com" + href, "scan": title.lower()}
        if new_links == 0:
            break


def adapter_euraxess():
    # Euraxess: location is in "Number of offers: N, Country, Institution, City, ..." block.
    seen = set()
    LOC_RX = re.compile(r'Number of offers:\s*\d+,\s*([A-Z][^,]+)', re.S)
    for pg in range(MAXPAGES):
        h = curl_get(EURAXESS_URLS[0] + f"&page={pg}")
        if not h:
            break
        new_links = 0
        for m in re.finditer(r'href="/jobs/(\d+)"[^>]*><span>([^<]+)</span>', h):
            jid, title = m.group(1), html.unescape(m.group(2)).strip()
            if jid in seen:
                continue
            seen.add(jid); new_links += 1
            if not ROLE_RE.search(title):
                continue
            after = h[m.end():m.end()+3000]
            lm = LOC_RX.search(after)
            loc = lm.group(1).strip().title() if lm else "EU"
            yield {"source": "Euraxess", "org": "Euraxess", "ext_id": jid, "title": title[:130],
                   "loc": loc, "deadline": "", "url": "https://euraxess.ec.europa.eu/jobs/" + jid,
                   "scan": title.lower()}
        if new_links == 0:
            break


def adapter_ebrd():
    yield from _paginate("EBRD",
        lambda n: f"https://jobs.ebrd.com/tile-search-results/?q=&startrow={(n - 1) * 10}",
        r'class="jobTitle-link[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        "https://jobs.ebrd.com",
        lambda h: (re.search(r"/(\d+)/?$", h).group(1) if re.search(r"/(\d+)/?$", h) else h),
        lambda h: (h.split("/job/")[1].split("-")[0] if "/job/" in h else ""))


def adapter_ceb():   # same SAP SuccessFactors platform as EBRD (empty when no open roles)
    yield from _paginate("CEB",
        lambda n: f"https://jobs.coebank.org/tile-search-results/?q=&startrow={(n - 1) * 10}",
        r'class="jobTitle-link[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        "https://jobs.coebank.org",
        lambda h: (re.search(r"/(\d+)/?$", h).group(1) if re.search(r"/(\d+)/?$", h) else h),
        lambda h: (h.split("/job/")[1].split("-")[0] if "/job/" in h else ""))


def adapter_aiib():
    # AIIB: current-jobs.js (structured JS) + static HTML (future opportunities).
    seen = set()
    # 1) Current opportunities from JS data file — split by jobs[N]=[]
    js = curl_get("https://www.aiib.org/en/opportunities/career/job-vacancies/staff/.content/index/current-jobs.js")
    def _field(block, key):
        m = re.search(rf'\["{key}"\]="([^"]*)"', block)
        return m.group(1).strip() if m else ""
    for block in re.split(r'jobs\[\d+\]=\[\];', js):
        title = re.sub(r"<[^>]+>", " ", _field(block, "title")).strip()
        if not title:
            continue
        dept = _field(block, "department")
        loc = _field(block, "location")
        path = _field(block, "path")
        dl = _field(block, "closing-date")
        slug = path.rsplit("/", 1)[-1].replace(".html", "") if path else title[:60]
        if slug in seen:
            continue
        seen.add(slug)
        if not ROLE_RE.search(title) and "econom" not in dept.lower():
            continue
        yield {"source": "AIIB", "org": "AIIB", "ext_id": slug, "title": title[:130],
               "loc": loc or "Beijing", "deadline": dl,
               "url": "https://www.aiib.org" + path if path.startswith("/") else path,
               "scan": (title + " " + dept).lower(), "extra": [dept] if dept else []}
    # 2) Future opportunities from static HTML
    h = curl_get("https://www.aiib.org/en/opportunities/career/job-vacancies/staff/index.html")
    for m in re.finditer(
            r'<div\s+class="pCopy1">([^<]+)</div>\s*'
            r'(?:.*?<a[^>]*href="(/en/opportunities/career/job-vacancies/staff/future-job-details/[^"]+)")?'
            r'(?:.*?<div\s+class="jType">([^<]*)</div>)?',
            h, re.S):
        title = html.unescape(m.group(1)).strip()
        href = m.group(2) or ""
        dept = (m.group(3) or "").strip()
        slug = href.rsplit("/", 1)[-1].replace(".html", "") if href else title.lower().replace(" ", "-")[:60]
        if slug in seen:
            continue
        seen.add(slug)
        if not ROLE_RE.search(title) and "econom" not in dept.lower():
            continue
        yield {"source": "AIIB", "org": "AIIB", "ext_id": slug, "title": title[:130],
               "loc": "Beijing", "deadline": "",
               "url": "https://www.aiib.org" + href if href else "",
               "scan": (title + " " + dept).lower(), "extra": [dept] if dept else []}


def adapter_adb():
    """ADB careers: Drupal table at /work-with-us/careers/current-opportunities."""
    h = urllib_get("https://www.adb.org/work-with-us/careers/current-opportunities")
    if not h: return
    for m in re.finditer(r'<a href="(https://www\.adb\.org/careers/(\d+))">([^<]+)</a>', h):
        url, jid, title = m.group(1), m.group(2), html.unescape(m.group(3)).strip()
        if not ROLE_RE.search(title): continue
        # extract location from row
        idx = m.start()
        row_s = h.rfind("<tr", 0, idx)
        row_e = h.find("</tr>", idx)
        row = h[row_s:row_e] if row_s >= 0 else ""
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        loc = re.sub(r"<[^>]+>", "", cells[3]).strip() if len(cells) > 3 else "Manila"
        dl = re.sub(r"<[^>]+>", "", cells[5]).strip() if len(cells) > 5 else ""
        yield {"source": "ADB", "org": "ADB", "ext_id": jid, "title": title[:130],
               "loc": loc, "deadline": dl, "url": url, "scan": title.lower()}


def adapter_bstdb():
    """BSTDB careers: Drupal views table at /career."""
    h = curl_get("https://www.bstdb.org/career")
    if not h: return
    tbl = re.search(r"current-vacancies-table.*?</table>", h, re.S)
    if not tbl: return
    block = tbl.group(0)
    for rm in re.finditer(r"<tr[^>]*>(.*?)</tr>", block, re.S):
        row = rm.group(1)
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 3: continue
        title = re.sub(r"<[^>]+>", " ", cells[1]).strip()
        status = re.sub(r"<[^>]+>", "", cells[3]).strip() if len(cells) > 3 else ""
        if "filled" in status.lower(): continue
        dl = re.sub(r"<[^>]+>", "", cells[2]).strip() if len(cells) > 2 else ""
        jid = re.sub(r"<[^>]+>", "", cells[0]).strip()
        if not ROLE_RE.search(title): continue
        yield {"source": "BSTDB", "org": "BSTDB", "ext_id": jid, "title": title[:130],
               "loc": "Thessaloniki", "deadline": dl, "url": "https://www.bstdb.org/career",
               "scan": title.lower()}


def adapter_bis():
    """BIS: vacancy cards at /about/careers/vacancies (Basel)."""
    h = curl_get("https://www.bis.org/about/careers/vacancies")
    if not h: return
    seen = set()
    for m in re.finditer(r'href="(/vacancy/(jr\d+))"', h):
        href, jid = m.group(1), m.group(2)
        if jid in seen: continue
        seen.add(jid)
        after = h[m.end():m.end()+800]
        tm = re.search(r'card-heading[^>]*>([^<]+)<', after)
        title = html.unescape(tm.group(1)).strip() if tm else jid
        if not ROLE_RE.search(title): continue
        yield {"source": "BIS", "org": "BIS", "ext_id": jid, "title": title[:130],
               "loc": "Basel", "deadline": "", "url": "https://www.bis.org" + href,
               "scan": title.lower()}


def adapter_hkma():
    """HKMA: direct HTML vacancy listing (Hong Kong)."""
    h = curl_get("https://www.hkma.gov.hk/eng/about-us/join-us/current-vacancies/")
    if not h: return
    seen = set()
    for m in re.finditer(r'href="(/eng/about-us/join-us/current-vacancies/(recruit-[^/"]+)/)"[^>]*>([^<]+)<', h):
        href, rid, title = m.group(1), m.group(2), html.unescape(m.group(3)).strip()
        if rid in seen: continue
        seen.add(rid)
        if not ROLE_RE.search(title): continue
        yield {"source": "HKMA", "org": "HKMA", "ext_id": rid, "title": title[:130],
               "loc": "Hong Kong", "deadline": "",
               "url": "https://www.hkma.gov.hk" + href, "scan": title.lower()}


def adapter_jpal():
    """J-PAL: structured job listings at /careers/...-job-NNNNN."""
    h = curl_get("https://www.povertyactionlab.org/careers")
    if not h: return
    seen = set()
    for m in re.finditer(r'href="(/careers/([^"]+job-(\d+)))"[^>]*>([^<]+)<', h):
        href, slug, jid, title = m.group(1), m.group(2), m.group(3), html.unescape(m.group(4)).strip()
        if jid in seen: continue
        seen.add(jid)
        if not ROLE_RE.search(title): continue
        loc = ""
        after = h[m.end():m.end()+500]
        lm = re.search(r'job-teaser-country.*?<li>([^<]+)</li>', after, re.S)
        if lm: loc = lm.group(1).strip()
        yield {"source": "J-PAL", "org": "J-PAL", "ext_id": jid, "title": title[:130],
               "loc": loc, "deadline": "", "url": "https://www.povertyactionlab.org" + href,
               "scan": title.lower()}


def adapter_unu_wider():
    """UNU-WIDER: opportunity listings."""
    h = curl_get("https://www.wider.unu.edu/opportunities")
    if not h: return
    seen = set()
    for m in re.finditer(r'href="(/opportunity/([^"]+))"[^>]*>([^<]+)<', h):
        href, slug, title = m.group(1), m.group(2), html.unescape(m.group(3)).strip()
        if slug in seen: continue
        seen.add(slug)
        if not ROLE_RE.search(title): continue
        yield {"source": "UNU-WIDER", "org": "UNU-WIDER", "ext_id": slug, "title": title[:130],
               "loc": "Helsinki", "deadline": "", "url": "https://www.wider.unu.edu" + href,
               "scan": title.lower()}


def adapter_bof():
    """Bank of Finland: vacancies on rekrytointi.com (external recruiting portal)."""
    h = curl_get("https://www.suomenpankki.fi/en/bank-of-finland/bank-of-finland-as-an-employer/job-opportunities/")
    if not h: return
    seen = set()
    for m in re.finditer(r'href="(https://suomenpankki\.rekrytointi\.com/paikat/[^"]+jid=(\d+)[^"]*)"[^>]*>([^<]+)<', h):
        url, jid, title = html.unescape(m.group(1)), m.group(2), html.unescape(m.group(3)).strip()
        if jid in seen: continue
        seen.add(jid)
        if not ROLE_RE.search(title): continue
        yield {"source": "Bank of Finland", "org": "Bank of Finland", "ext_id": jid,
               "title": title[:130], "loc": "Helsinki", "deadline": "", "url": url,
               "scan": title.lower()}


def adapter_cepal():
    """CEPAL/ECLAC: LinkedIn job links on employment page."""
    h = curl_get("https://www.cepal.org/en/about/employment")
    if not h: return
    seen = set()
    for m in re.finditer(r'href="(https://www\.linkedin\.com/[^"]+)"[^>]*>([^<]+)</a>', h):
        url, title = m.group(1), html.unescape(m.group(2)).strip()
        if len(title) < 10: continue
        eid = re.search(r"activity:(\d+)", url)
        slug = eid.group(1) if eid else title[:40]
        if slug in seen: continue
        seen.add(slug)
        if not ROLE_RE.search(title): continue
        yield {"source": "CEPAL", "org": "CEPAL/ECLAC", "ext_id": slug, "title": title[:130],
               "loc": "Santiago", "deadline": "", "url": url, "scan": title.lower()}


def adapter_wzb():
    """WZB Berlin: Drupal job listing page (requires curl -k for SSL)."""
    try:
        r = subprocess.run(["curl", "-skL", "-A", UA, "--max-time", "15",
                            "https://www.wzb.eu/en/the-wzb/working-at-the-wzb/job-opportunities"],
                           capture_output=True, timeout=25)
        h = r.stdout.decode(errors="replace")
    except Exception:
        return
    if not h: return
    seen = set()
    for m in re.finditer(r'href="(/en/jobs/([^"]+))"[^>]*>([^<]+)<', h):
        href, slug, title = m.group(1), m.group(2), html.unescape(m.group(3)).strip()
        if slug in seen: continue
        seen.add(slug)
        if not ROLE_RE.search(title): continue
        yield {"source": "WZB", "org": "WZB", "ext_id": slug, "title": title[:130],
               "loc": "Berlin", "deadline": "", "url": "https://www.wzb.eu" + href,
               "scan": title.lower()}


def adapter_ecorys():
    """Ecorys: TeamTailor RSS feed."""
    h = curl_get("https://careers.ecorys.com/en/jobs.rss")
    if not h: return
    seen = set()
    for m in re.finditer(r"<item>.*?<title>([^<]+)</title>.*?<link>([^<]+)</link>", h, re.S):
        title, url = html.unescape(m.group(1)).strip(), m.group(2).strip()
        jid = re.search(r"/jobs/(\d+)", url)
        eid = jid.group(1) if jid else title[:40]
        if eid in seen: continue
        seen.add(eid)
        if not ROLE_RE.search(title): continue
        yield {"source": "Ecorys", "org": "Ecorys", "ext_id": eid, "title": title[:130],
               "loc": "Rotterdam", "deadline": "", "url": url, "scan": title.lower()}


def adapter_cra():
    """Charles River Associates: Greenhouse API, PhD filter."""
    url = "https://boards-api.greenhouse.io/v1/boards/charlesriverassociates/jobs?content=true"
    try:
        data = json.loads(urllib_get(url, timeout=15))
    except Exception:
        return
    for j in data.get("jobs", []):
        title = j.get("title", "")
        if "phd" not in title.lower() and "ph.d" not in title.lower():
            continue
        if INTERN_EXCLUDE.search(title): continue
        loc = j.get("location", {}).get("name", "")
        jid = str(j.get("id", ""))
        jurl = j.get("absolute_url", "")
        yield {"source": "CRA", "org": "CRA", "ext_id": jid, "title": title[:130],
               "loc": loc, "deadline": "", "url": jurl, "scan": title.lower()}


def adapter_crei():
    """CREI Barcelona: links to euraxess (deduped by euraxess job id)."""
    h = urllib_get("https://crei.cat/work-with-us/")
    if not h: return
    seen = set()
    for m in re.finditer(r'href="(https://euraxess\.ec\.europa\.eu/jobs/(\d+))"[^>]*>([^<]+)<', h):
        url, jid, title = m.group(1), m.group(2), html.unescape(m.group(3)).strip()
        if jid in seen: continue
        seen.add(jid)
        if not ROLE_RE.search(title): continue
        yield {"source": "CREI", "org": "CREI", "ext_id": jid, "title": title[:130],
               "loc": "Barcelona", "deadline": "", "url": url, "scan": title.lower()}


def adapter_siaw():
    """St Gallen SIAW: links to jobs.unisg.ch."""
    h = urllib_get("https://siaw.unisg.ch/en/the-institute/job-openings/")
    if not h: return
    seen = set()
    for m in re.finditer(r'href="(https://jobs\.unisg\.ch/[^"]+/([^/"]+))"[^>]*>([^<]+)<', h):
        url, slug, title = m.group(1), m.group(2), html.unescape(m.group(3)).strip()
        if slug in seen: continue
        seen.add(slug)
        if not ROLE_RE.search(title): continue
        yield {"source": "SIAW", "org": "U St Gallen (SIAW)", "ext_id": slug, "title": title[:130],
               "loc": "St Gallen", "deadline": "", "url": url, "scan": title.lower()}


def adapter_unu_careers():
    """UNU careers: careers.unu.edu job listings."""
    h = urllib_get("https://careers.unu.edu/")
    if not h: return
    seen = set()
    for m in re.finditer(r'href="(/o/[^"]+)"[^>]*>([^<]+)<', h):
        href, title = m.group(1), html.unescape(m.group(2)).strip()
        slug = href.split("/")[-1] or href
        if slug in seen: continue
        seen.add(slug)
        if not ROLE_RE.search(title): continue
        yield {"source": "UNU", "org": "UNU", "ext_id": slug, "title": title[:130],
               "loc": "", "deadline": "", "url": "https://careers.unu.edu" + href, "scan": title.lower()}


def adapter_brattle():
    """The Brattle Group: Greenhouse API."""
    url = "https://boards-api.greenhouse.io/v1/boards/thebrattlegroup/jobs?content=true"
    try:
        data = json.loads(urllib_get(url, timeout=15))
    except Exception:
        return
    for j in data.get("jobs", []):
        title = j.get("title", "")
        if not ROLE_RE.search(title): continue
        if INTERN_EXCLUDE.search(title): continue
        loc = j.get("location", {}).get("name", "")
        jid = str(j.get("id", ""))
        jurl = j.get("absolute_url", "")
        yield {"source": "Brattle", "org": "Brattle", "ext_id": jid, "title": title[:130],
               "loc": loc, "deadline": "", "url": jurl, "scan": title.lower()}


def adapter_moodys():
    """Moody's: economist search results page."""
    h = curl_get("https://careers.moodys.com/en/search-jobs/economist/49841/1")
    if not h: return
    seen = set()
    for m in re.finditer(r'href="(/en/job/([^/]+)/([^/]+)/49841/(\d+))"', h):
        href, city, slug, jid = m.group(1), m.group(2), m.group(3), m.group(4)
        if jid in seen: continue
        seen.add(jid)
        title = slug.replace("-", " ").title()
        if INTERN_EXCLUDE.search(title): continue
        yield {"source": "Moodys", "org": "Moody's", "ext_id": jid, "title": title[:130],
               "loc": city.replace("-", " ").title(), "deadline": "",
               "url": "https://careers.moodys.com" + href, "scan": title.lower()}


def adapter_cgd():
    """CGD: applytojob.com links from careers page."""
    h = curl_get("https://www.cgdev.org/careers")
    if not h: return
    seen = set()
    for m in re.finditer(r'href="(https://centerforglobaldevelopment\.applytojob\.com/apply/([^/"]+)/[^"]*)"[^>]*>([^<]+)<', h):
        url, jid, title = m.group(1), m.group(2), html.unescape(m.group(3)).strip()
        if jid in seen: continue
        seen.add(jid)
        if INTERN_EXCLUDE.search(title): continue
        if not ROLE_RE.search(title): continue
        yield {"source": "CGD", "org": "CGD", "ext_id": jid, "title": title[:130],
               "loc": "Washington DC", "deadline": "", "url": url, "scan": title.lower()}


def adapter_safra():
    """Bank J. Safra Sarasin: Umantis portal."""
    h = curl_get("https://jsafrasarasin.umantis.com/Jobs/All")
    if not h: return
    seen = set()
    for m in re.finditer(r'href="(/Vacancies/(\d+)/Description/\d+)"[^>]*>([^<]+)<', h):
        href, vid, title = m.group(1), m.group(2), html.unescape(m.group(3)).strip()
        if vid in seen: continue
        seen.add(vid)
        if not ROLE_RE.search(title): continue
        yield {"source": "Safra", "org": "Bank J. Safra Sarasin", "ext_id": vid, "title": title[:130],
               "loc": "Basel", "deadline": "",
               "url": "https://jsafrasarasin.umantis.com" + href, "scan": title.lower()}


def adapter_ifo():
    """ifo Institute: Drupal job-offer links."""
    h = curl_get("https://www.ifo.de/en/career-ifo")
    if not h: return
    seen = set()
    for m in re.finditer(r'href="(/en/job-offer/([^"]+))"[^>]*>([^<]+)<', h):
        href, slug, title = m.group(1), m.group(2), html.unescape(m.group(3)).strip()
        if slug in seen: continue
        seen.add(slug)
        if INTERN_EXCLUDE.search(title): continue
        yield {"source": "ifo", "org": "ifo Institute", "ext_id": slug, "title": title[:130],
               "loc": "Munich", "deadline": "", "url": "https://www.ifo.de" + href, "scan": title.lower()}


def adapter_cmcc():
    """CMCC: applytojob.com links (class="title") from careers page."""
    h = urllib_get("https://www.cmcc.it/about/careers")
    if not h: return
    seen = set()
    for m in re.finditer(r'class="title"\s+href="(https://cmccfoundation\.applytojob\.com/apply/([^/"]+)/[^"]*)"[^>]*>([^<]+)<', h):
        url, jid, title = m.group(1), m.group(2), html.unescape(m.group(3)).strip()
        if jid in seen: continue
        seen.add(jid)
        if INTERN_EXCLUDE.search(title): continue
        # ponytail: no ROLE_RE — small climate-econ org, show all non-intern posts
        yield {"source": "CMCC", "org": "CMCC", "ext_id": jid, "title": title[:130],
               "loc": "Italy", "deadline": "", "url": url, "scan": title.lower()}


def adapter_wto_page():
    """WTO: static vacancy page (supplements Workday adapter for non-ATS listings)."""
    h = curl_get("https://www.wto.org/english/thewto_e/vacan_e/vacan_e.htm")
    if not h: return
    seen = set()
    for m in re.finditer(r'href="([^"]*vacan[^"]+\.htm)"[^>]*>([^<]{10,})<', h):
        href, title = m.group(1), html.unescape(m.group(2)).strip()
        if not re.search(r"econom|research|statistic|analyst", title, re.I): continue
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower())[:60]
        if slug in seen: continue
        seen.add(slug)
        base = "https://www.wto.org/english/thewto_e/vacan_e/"
        url = base + href if not href.startswith("http") else href
        yield {"source": "WTO", "org": "WTO", "ext_id": slug, "title": title[:130],
               "loc": "Geneva", "deadline": "", "url": url, "scan": title.lower()}


# ── generic career-page scraper for sites with simple HTML job listings ──────
# Each entry: (org, careers_url, location, link_pattern, base_url)
# link_pattern: regex with group(1)=href, group(2)=title (or last named group)
GENERIC_SITES = [
    # Think tanks
    ("Bruegel", "https://www.bruegel.org/careers", "Brussels",
     r'href="(https://www\.bruegel\.org/careers/[^"]+)"[^>]*>([^<]+)<'),
    ("Kiel Institute", "https://www.kielinstitut.de/institute/career/job-vacancies/", "Kiel",
     r'href="(/institute/career/job-vacancies/[^"]+)"[^>]*>([^<]+)<'),
    ("DIW", "https://www.diw.de/en/diw_01.c.10197.en/careers.html", "Berlin",
     r'href="(/de/diw_01\.c\.\d+\.de/[^"]*jobs?[^"]*\.html)"[^>]*>([^<]+)<'),
    ("ZEW", "https://www.zew.de/en/career", "Mannheim",
     r'href="(/en/career/[^"]+)"[^>]*>([^<]+)<'),
    ("RFF", "https://www.rff.org/about/careers/", "Washington DC",
     r'href="(https://www\.rff\.org/careers/[^"]+)"[^>]*>([^<]+)<'),
    ("Grantham/LSE", "https://www.lse.ac.uk/granthaminstitute/about/vacancies/", "London",
     r'href="(https://jobs\.lse\.ac\.uk/[^"]+)"[^>]*>([^<]+)<'),
    ("PIK", "https://www.pik-potsdam.de/en/people/career", "Potsdam",
     r'href="(/en/people/career/[^"]+)"[^>]*>([^<]+)<'),
    ("WRI", "https://www.wri.org/careers", "Washington DC",
     r'href="(https://jobs\.jobvite\.com/wri/[^"]+)"[^>]*>([^<]+)<'),
    ("PIIE", "https://www.piie.com/careers", "Washington DC",
     r'href="(https://www\.piie\.com/careers/[^"]+)"[^>]*>([^<]+)<'),
    # Consulting
    ("Frontier Economics", "https://www.frontier-economics.com/uk/en/careers/", "London/Zurich",
     r'href="(/uk/en/careers/(?:vacancies|experienced|analysts)[^"]*)"[^>]*>([^<]+)<'),
    ("London Economics", "https://londoneconomics.co.uk/recruitment/", "London",
     r'href="(https://londoneconomics\.co\.uk/recruitment/[^"]+)"[^>]*>([^<]+)<'),
    ("Cambridge Econometrics", "https://www.camecon.com/careers/", "Cambridge",
     r'href="(https://www\.camecon\.com/careers?/[^"]+)"[^>]*>([^<]+)<'),
    ("RBB Economics", "https://www.rbbecon.com/careers/", "Various",
     r'href="(https://www\.rbbecon\.com/careers/[^"]+)"[^>]*>([^<]+)<'),
    ("E.CA Economics", "https://e-ca.com/careers", "Berlin",
     r'href="(https://e-ca\.com/careers/[^"]+)"[^>]*>([^<]+)<'),
    ("Copenhagen Economics", "https://copenhageneconomics.com/careers/working-with-us", "Copenhagen",
     r'href="(https://copenhageneconomics\.com/careers/[^"]+)"[^>]*>([^<]+)<'),
    ("NERA", "https://www.nera.com/careers.html", "Various",
     r'href="(https://www\.nera\.com/careers/[^"]+)"[^>]*>([^<]+)<'),
    # Private sector / energy
    ("Trafigura", "https://www.trafigura.com/careers/", "Geneva",
     r'href="(https://www\.trafigura\.com/careers/[^"]+)"[^>]*>([^<]+)<'),
    ("Wood Mackenzie", "https://www.woodmac.com/careers/", "Various",
     r'href="(https://www\.woodmac\.com/careers/[^"]+)"[^>]*>([^<]+)<'),
    ("Aurora Energy", "https://auroraer.com/careers/", "Oxford",
     r'href="(https://auroraer\.com/careers/[^"]+)"[^>]*>([^<]+)<'),
    ("Rystad Energy", "https://www.rystadenergy.com/careers", "Oslo",
     r'href="(https://www\.rystadenergy\.com/careers/[^"]+)"[^>]*>([^<]+)<'),
    ("Mirabaud", "https://www.mirabaud.com/en/careers/", "Geneva",
     r'href="(https://www\.mirabaud\.com/en/careers/[^"]+)"[^>]*>([^<]+)<'),
    # Research centers
    ("KOF/ETH", "https://kof.ethz.ch/en/the-institute/open-positions.html", "Zurich",
     r'href="(/en/the-institute/open-positions/[^"]+)"[^>]*>([^<]+)<'),
    ("UZH Econ", "https://www.econ.uzh.ch/en/about/jobs.html", "Zurich",
     r'href="(/en/about/jobs/[^"]+)"[^>]*>([^<]+)<'),
    ("ETH jobs", "https://jobs.ethz.ch/site/index", "Zurich",
     r'href="(/job/view/[^"]+)"[^>]*>([^<]+)<'),
    ("EPFL", "https://www.epfl.ch/about/working/page-26457-en-html/", "Lausanne",
     r'href="(https://www\.epfl\.ch/about/working/faculty-posit[^"]+)"[^>]*>([^<]+)<'),
    ("TSE", "https://www.tse-fr.eu/join-toulouse-school-economics", "Toulouse",
     r'href="(/[^"]*(?:job|position|recruitment|hiring)[^"]*)"[^>]*>([^<]+)<'),
    ("EUI", "https://www.eui.eu/About/JobOpportunities", "Florence",
     r'href="(/en/[^"]*(?:job|position|vacanc|apply)[^"]*)"[^>]*>([^<]+)<'),
    ("IHEID", "https://www.graduateinstitute.ch/open-positions", "Geneva",
     r'href="(/open-positions/[^"]+)"[^>]*>([^<]+)<'),
    ("IPA", "https://poverty-action.org/careers", "Various",
     r'href="(/careers/[^"]+)"[^>]*>([^<]+)<'),
    # Swiss boutiques
    ("Swiss Economics", "https://www.swiss-economics.ch/en/team/", "Zurich",
     r'href="(https://www\.swiss-economics\.ch/en/[^"]*(?:job|career|stell)[^"]*)"[^>]*>([^<]+)<'),
    # Central banks
    ("Banco de Espana", "https://www.bde.es/wbe/en/sobre-banco/trabajar-banco/", "Madrid",
     r'href="([^"]*(?:convocatoria|vacante|oferta)[^"]*)"[^>]*>([^<]+)<'),
    ("NBB Belgium", "https://jobs.nbb.be/en", "Brussels",
     r'href="(/en/[^"]*(?:job|vacanc)[^"]+)"[^>]*>([^<]+)<'),
]


def adapter_generic():
    """Scrape career pages listed in GENERIC_SITES."""
    for org, url, loc, pat in GENERIC_SITES:
        try:
            h = curl_get(url, timeout=15)
            if not h:
                h = urllib_get(url)
            if not h: continue
        except Exception:
            continue
        seen = set()
        rx = re.compile(pat, re.S)
        for m in rx.finditer(h):
            href = html.unescape(m.group(1)).strip()
            title = html.unescape(m.group(2)).strip()
            if len(title) < 8 or len(title) > 120: continue
            # skip nav/info links
            if re.search(r"cookie|privacy|contact us|about us|^home$|^menu$|sign in|newsletter|terms", title, re.I):
                continue
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower())[:60]
            if slug in seen: continue
            seen.add(slug)
            if not ROLE_RE.search(title): continue
            full_url = href if href.startswith("http") else (url.rsplit("/", 1)[0] + "/" + href.lstrip("/"))
            yield {"source": org, "org": org, "ext_id": slug, "title": title[:130],
                   "loc": loc, "deadline": "", "url": full_url, "scan": title.lower()}


def adapter_impactpool(cap=40):
    # UN/IO aggregator: each card has cardTitle + 3x bodyEmphasis (employer, location, level).
    seen = set(); n = 0
    rx = re.compile(r'href="(/jobs/(\d+))"[^>]*>\s*<h3[^>]*type=.cardTitle.>([^<]+)</h3>'
                    r'(?:.*?type=.bodyEmphasis.>\s*([^<]+?)\s*<)?'   # employer
                    r'(?:.*?type=.bodyEmphasis.>\s*([^<]+?)\s*<)?'   # location
                    r'(?:.*?type=.bodyEmphasis.>\s*([^<]+?)\s*<)?',  # level/grade
                    re.S)
    NATIONAL = re.compile(r"national|locally recruited", re.I)
    for term in IMPACTPOOL_SEARCHES:
        if n >= cap:
            break
        h = curl_get("https://www.impactpool.org/search?q=" + term.replace(" ", "+"))
        for m in rx.finditer(h):
            href, jid = m.group(1), m.group(2)
            title = html.unescape(m.group(3).strip())
            emp = html.unescape((m.group(4) or "").strip())
            loc = html.unescape((m.group(5) or "").strip())
            level = html.unescape((m.group(6) or "").strip())
            if jid in seen or not ROLE_RE.search(title):
                continue
            seen.add(jid); n += 1
            if title.isupper():
                title = title.title()
            national = " [National Staff]" if NATIONAL.search(level) else ""
            yield {"source": "Impactpool", "org": emp or "Impactpool", "ext_id": jid, "title": title[:130] + national,
                   "loc": loc, "deadline": "", "url": "https://www.impactpool.org" + href, "scan": (title + " " + emp).lower(),
                   "extra": [level] if level else []}
            if n >= cap:
                break


def adapter_cagi():
    # CAGI Geneva NGO board — WordPress, all Geneva. Low volume but Tier 1 location.
    h = curl_get("https://jobs.cagi.ch/jobs/")
    seen = set()
    for m in re.finditer(r'class="job-title"><a href="(https://jobs\.cagi\.ch/job/([^/]+)/)"[^>]*>([^<]+)</a>', h):
        url, slug, title = m.group(1), m.group(2), html.unescape(m.group(3)).strip()
        if slug in seen or not ROLE_RE.search(title):
            continue
        seen.add(slug)
        # employer from URL slug: {org}-geneva-{contract}-{title}
        parts = slug.split("-geneva-")
        org = parts[0].replace("-", " ").title() if parts else "CAGI"
        yield {"source": "CAGI", "org": org, "ext_id": slug, "title": title[:130],
               "loc": "Geneva", "deadline": "", "url": url, "scan": title.lower()}


RELIEFWEB_SEARCHES = ["economist", "economics", "economic affairs"]


def adapter_reliefweb(cap=40):
    # ReliefWeb: server-rendered HTML with article blocks (country, employer, deadline).
    seen = set(); n = 0
    ART = re.compile(r'<article[^>]*data-id="(\d+)".*?</article>', re.S)
    TITLE = re.compile(r'rw-river-article__title[^>]*><a href="([^"]+)">([^<]+)</a>')
    COUNTRY = re.compile(r'rw-entity-country-slug__link[^>]*>([^<]+)<')
    ORG = re.compile(r'tag-value--source.*?tag-link[^>]*>([^<]+)<', re.S)
    CLOSING = re.compile(r'tag-label--closing-date.*?datetime="([^"]+)"', re.S)
    for term in RELIEFWEB_SEARCHES:
        if n >= cap:
            break
        for pg in range(0, 3):
            url = f"https://reliefweb.int/jobs?search={term.replace(' ', '+')}" + (f"&page={pg}" if pg else "")
            h = curl_get(url)
            hit = 0
            for am in ART.finditer(h):
                aid = am.group(1)
                block = am.group(0)
                tm = TITLE.search(block)
                if not tm or aid in seen:
                    continue
                seen.add(aid); hit += 1
                title = html.unescape(tm.group(2)).strip()
                job_url = tm.group(1)
                cm = COUNTRY.search(block)
                country = cm.group(1).strip() if cm else "Remote / TBD"
                om = ORG.search(block)
                org = om.group(1).strip() if om else ""
                dl_m = CLOSING.search(block)
                dl = dl_m.group(1)[:10] if dl_m else ""
                if not ROLE_RE.search(title):
                    continue
                n += 1
                yield {"source": "ReliefWeb", "org": org or "ReliefWeb", "ext_id": aid, "title": title[:130],
                       "loc": country, "deadline": dl, "url": job_url, "scan": title.lower()}
                if n >= cap:
                    break
            if hit == 0 or n >= cap:
                break


# ── IMAP email-alert adapter: reads job-alert emails from Gmail inbox ───────
# Senders whose alert emails we parse. Each: (label, from_pattern, link_regex).
# Sign up for alerts on each site; the adapter auto-ingests them.
# (label, from_pattern, link_regex, broad_alert)
# broad_alert=True means the alert is keyword-broad → apply ROLE_RE filter
ALERT_SENDERS = [
    ("Academic Positions", "academicpositions",
     re.compile(r'href="(https://academicpositions\.com/ad/([^/]+)/\d+/[^/]+/(\d+)[^"]*)"[^>]*>([^<]+)<', re.S), False),
    ("Devex", "devex.com",
     re.compile(r'href="(https://www\.devex\.com/jobs/([^/]+)-(\d+)[^"]*)"[^>]*>\s*([^<]+?)\s*<', re.S), True),
    ("EuroBrussels", "eurobrussels",
     re.compile(r'href="(https?://(?:www2?\.)?eurobrussels\.com/job_display/(\d+)/[^"]*)"[^>]*>(?:\s|<[^>]*>)*([^<]{5,}?)\s*<', re.S), True),
    ("IDB", "interameri",
     re.compile(r'href="(https?://[^"]*iadb\.org[^"]*?/([^/"]+?)(?:\.html)?)"[^>]*>([^<]{5,})<', re.S), True),
    ("NBB", "nbbbnb",
     re.compile(r'href="(https?://nbb\.jobs\.hr\.cloud\.sap/job/[^"]+?/(\d+)-[^"]*)"[^>]*>\s*([^<]+?)\s*<', re.S), True),
    ("NDB", "NDB Recruitment",
     re.compile(r'href="(https://career\d+\.sapsf\.cn/career\?company=newdevelop[^"]*career%5fjob%5freq%5fid=(\d+)[^"]*)"[^>]*>\s*([^<]+?)\s*<', re.S), True),
    ("unvacancies", "unvacancies",
     re.compile(r'href="(https://unvacancies\.org/jobs/([a-z0-9]+-[a-z0-9-]+-\d+)[^"]*)"[^>]*>(?:\s|<[^>]*>)*([^<]{5,}?)\s*<', re.S), True),
    ("World Bank", "worldbankgroup", None, True),
    ("Swiss Universities", "prospective.ch",
     re.compile(r'href="(https://([^/]+)/(?:job-vacancies|offene-stellen)/([^/?]+)/[^"]*)"[^>]*>\s*([^<]{5,}?)\s*<', re.S), True),
]
# Broad extractor for alerts without a bespoke pattern: any anchor (10-140 chars) that survives ROLE_RE.
# ponytail: catch-all pattern → ROLE_RE keeps only econ-relevant titles, so the noise self-filters.
_GENERIC_ANCHOR = re.compile(
    r'href="(https?://[^"]{20,}?)"[^>]*>\s*(?:<[^>]*>\s*)*([A-Za-z][^<]{8,140}?)\s*<', re.S)
# Senders Marcelo subscribed to that need extraction + inbox cleanup. Extraction is best-effort;
# what matters most is that they get archived so the inbox stops filling. Add new alerts here as
# he signs up for them — pattern is either the sender's display name or a substring of its email.
_MORE_ALERTS = [   # patterns tested against Gmail IMAP FROM: use full email or token-safe substrings.
    # Confirmed already firing (patterns verified against inbox):
    ("ITU", "itujobnotification"), ("Fitch", "fitchgroupincp-jobnotification"),
    ("WFP", "wfp@myworkday.com"), ("Mastercard", "talent@careers.mastercard.com"),
    ("NIB", "reachmee"), ("EIB", "jobs@eib.org"),
    ("UNIL", "UNIL Recrutement"), ("Pictet", "Pictet Recruitment"),
    # Registered in the Source Tracker but not yet firing — patterns are best guesses.
    # When alerts start arriving Marcelo, tell me the sender header and I'll tighten these.
    ("EU Careers", "eu-careers"), ("EU Careers 2", "epso"),
    ("80,000 Hours", "80000hours"), ("Idealist", "idealist"),
    ("Bern", "unibe.ch"), ("HSLU", "hslu"),
]
for _label, _pat in _MORE_ALERTS:
    ALERT_SENDERS.append((_label, _pat, _GENERIC_ANCHOR, True))
# Extra from-patterns to archive (registration confirmations, not recurring alerts)
ARCHIVE_EXTRA = ["candidatemanager", "substack.com", "teamtailor-mail", "careers.nbb.be"]
IMAP_DAYS = 7  # look back this many days


def _unwrap_tracking_hrefs(body):
    """Decode tracking-wrapped hrefs (Postmark, Mailjet, unvacancies link) to real URLs."""
    # Normalize single-quoted hrefs to double-quoted (WB CSOD uses single quotes)
    body = re.sub(r"href='([^']*)'", r'href="\1"', body)
    def _decode(m):
        decoded = urllib.parse.unquote(m.group(1))
        if not decoded.startswith("http"):
            decoded = "https://" + decoded
        return f'href="{decoded}"'
    # Postmark: track.pstmrk.it/CODE/URL-ENCODED/tracking-ids...
    body = re.sub(r'href="https://track\.pstmrk\.it/\w+/([^/"]+)[^"]*"', _decode, body)
    # unvacancies tracker
    body = re.sub(r'href="https://link\.unvacancies\.org/CL0/([^/"]+)[^"]*"', _decode, body)
    # Mailjet: *.mj.am/lnk/.../BASE64-ENCODED-URL
    def _mj(m):
        b = m.group(1)
        b += "=" * (-len(b) % 4)
        try:
            return f'href="{base64.urlsafe_b64decode(b).decode("utf-8", "replace")}"'
        except Exception:
            return m.group(0)
    body = re.sub(r'href="https?://\w+\.mj\.am/lnk/[^"]+/([A-Za-z0-9_-]{10,})"', _mj, body)
    return body


def adapter_ejm():
    """Scrape EconJobMarket.org positions (all pages)."""
    seen = set()
    clean_rx = re.compile(r'<!--\[if \w+\]>.*?<!\[endif\]-->', re.S)
    for pg in range(1, 10):
        url = "https://econjobmarket.org/positions" + (f"?page={pg}" if pg > 1 else "")
        try:
            raw = http_get(url)
        except Exception:
            break
        body = re.sub(r'\s+', ' ', clean_rx.sub(' ', raw))
        ids = re.findall(r'name="(\d{4,6})"', body)
        if not ids:
            break
        for pid in ids:
            if pid in seen:
                continue
            idx = body.find(f'name="{pid}"')
            chunk = body[idx:idx+2500]
            tm = re.search(r'>([^<]{3,120})</a>\s*<button', chunk)
            if not tm:
                continue
            title = html.unescape(tm.group(1).strip())
            if INTERN_EXCLUDE.search(title):
                continue
            seen.add(pid)
            lm = re.search(r'</button>\s*<br/?>\s*([A-Z][\w\s,.\'-]{2,80}?)(?:\s*\(|<br|\.?\s*<)', chunk)
            loc = lm.group(1).strip().rstrip('.') if lm else ""
            im = re.findall(r'media-body">\s*([^<]{3,80}?)\s*(?:<|$)', chunk)
            inst = im[1].strip() if len(im) > 1 else (im[0].strip() if im else "")
            type_m = re.search(r'col-md-2">\s*(.*?)<hr', chunk, re.S)
            types = [t.strip() for t in re.findall(r'([A-Z][A-Za-z /()-]{3,40})', type_m.group(1))
                     if t.strip() not in ("BLOCK", "ENDBLOCK")] if type_m else []
            dm = re.search(r'class="positive">(\d+ \w+ \d+)', chunk)
            deadline = dm.group(1) if dm else ""
            yield {"source": "EJM", "org": inst or "EJM", "ext_id": pid,
                   "title": title[:130], "loc": loc, "deadline": deadline,
                   "url": f"https://econjobmarket.org/positions/{pid}",
                   "scan": title.lower(), "ejm_types": types}


def adapter_joe():
    """Scrape AEA JOE listings (all pages)."""
    seen = set()
    url = "https://www.aeaweb.org/joe/listings"
    current_page = 1
    for _ in range(10):
        try:
            body = http_get(url)
        except Exception:
            break
        current_inst = ""
        found = 0
        for m in re.finditer(
            r'(?:group-header-title">([^<]+)</h\d>'
            r'|group-sub-header-title">([^<]+)</h\d>'
            r'|listing-item-header-title">\s*<div[^>]*>.*?</div>\s*'
            r'<a\s+href="(/joe/listing\.php\?JOE_ID=([^&]+)[^"]*)"[^>]*>([^<]+)</a>)',
            body, re.S
        ):
            if m.group(1):
                current_inst = html.unescape(m.group(1).strip())
                continue
            if m.group(2):
                continue
            joe_id = m.group(4)
            if joe_id in seen:
                continue
            title = html.unescape(m.group(5).strip())
            if INTERN_EXCLUDE.search(title):
                continue
            seen.add(joe_id)
            found += 1
            rest = body[m.end():m.end()+2000]
            sm = re.search(r'<span>Section:</span>\s*([^<]+)', rest)
            section = sm.group(1).strip() if sm else ""
            lm = re.search(r'<span>Location:</span>\s*([^<]+)', rest)
            loc = lm.group(1).strip() if lm else ""
            dm = re.search(r'Application deadline:\s*([^<]+)', rest)
            deadline = dm.group(1).strip() if dm else ""
            yield {"source": "JOE", "org": current_inst or "JOE", "ext_id": joe_id,
                   "title": title[:130], "loc": loc, "deadline": deadline,
                   "url": f"https://www.aeaweb.org/joe/listing.php?JOE_ID={joe_id}",
                   "scan": title.lower()}
        if found == 0:
            break
        pag = re.findall(r'<a[^>]*href="(/joe/listings\?q=[^"]+)"[^>]*>(\d+)</a>', body)
        next_url = None
        for href, label in pag:
            if int(label) == current_page + 1:
                next_url = f"https://www.aeaweb.org{html.unescape(href)}"
                break
        if not next_url:
            break
        url = next_url
        current_page += 1


OPEN_BOARDS = [
    ("https://www.openpostdoc.org/board?field=Economics", "OpenPostdoc", False),
    ("https://www.openpostdoc.org/board?field=Political+Science", "OpenPostdoc", True),
    ("https://www.openfacultyjobs.com/board?field=Economics", "OpenFacultyJobs", False),
    ("https://www.openfacultyjobs.com/board?field=Political+Science", "OpenFacultyJobs", True),
]

def adapter_open_boards():
    """Scrape openpostdoc.org and openfacultyjobs.com RSC flight data."""
    seen = set()
    for board_url, label, polsci_only in OPEN_BOARDS:
        try:
            body = http_get(board_url)
        except Exception:
            continue
        chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', body, re.S)
        if not chunks:
            continue
        raw = "".join(chunks).replace('\\"', '"').replace('\\n', '\n')
        for m in re.finditer(r'\{"id":"([a-f0-9-]{36})"', raw):
            start = m.start()
            depth = 0
            end = start
            for i in range(start, min(start + 5000, len(raw))):
                if raw[i] == '{': depth += 1
                elif raw[i] == '}': depth -= 1
                if depth == 0:
                    end = i + 1
                    break
            try:
                obj = json.loads(raw[start:end])
            except Exception:
                continue
            title = obj.get("title", "")
            if not title or "institutionName" not in obj:
                continue
            jid = obj["id"]
            if jid in seen:
                continue
            if polsci_only and "political economy" not in title.lower():
                continue
            if not ROLE_RE.search(title):
                continue
            if ACAD_EXCLUDE.search(title) or INTERN_EXCLUDE.search(title):
                continue
            seen.add(jid)
            inst = obj.get("institutionName", "")
            city = obj.get("city") or ""
            country = obj.get("country") or ""
            loc = f"{city}, {country}".strip(", ") if city else country
            url = obj.get("applyUrl") or obj.get("sourceUrl") or ""
            deadline = obj.get("deadlineDate") or ""
            primary = obj.get("primaryField") if obj.get("primaryField") != "Economics" else ""   # econ is the default here
            field = " · ".join(x for x in (primary, obj.get("subField")) if x)
            extra = [x for x in (field, obj.get("labName"), "Starts " + obj["startDate"][:10] if obj.get("startDate") else "") if x]
            yield {"source": label, "org": inst, "ext_id": jid, "title": title[:130],
                   "loc": loc, "deadline": deadline, "url": url, "scan": title.lower(), "extra": extra}


def adapter_imap():
    creds = load_creds()
    addr = creds.get("email.address", "")
    pw = creds.get("email.app_password", "").replace(" ", "")
    if not addr or not pw:
        return
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=CTX)
        M.login(addr, pw)
        M.select("INBOX", readonly=True)
    except Exception:
        return
    since = (datetime.now() - timedelta(days=IMAP_DAYS)).strftime("%d-%b-%Y")
    seen = set()
    for label, from_pat, link_rx, broad in ALERT_SENDERS:
        try:
            _, nums = M.search(None, f'(FROM "{from_pat}" SINCE {since})')
        except Exception:
            continue
        for num in (nums[0] or b"").split():
            try:
                _, data = M.fetch(num, "(RFC822)")
                msg = emailmod.message_from_bytes(data[0][1])
            except Exception:
                continue
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    ct = part.get_content_type()
                    if ct == "text/html":
                        body = part.get_payload(decode=True).decode("utf-8", "replace")
                        break
                    if ct == "text/plain" and not body:
                        body = part.get_payload(decode=True).decode("utf-8", "replace")
            else:
                body = msg.get_payload(decode=True).decode("utf-8", "replace")
            body = _unwrap_tracking_hrefs(body)
            if link_rx is None:
                # Custom extraction for linkless/structured alerts (World Bank CSOD)
                _wb_rx = re.compile(
                    r"font-weight:\s*700[^>]*>([^<]{5,120})</p>\s*<p[^>]*>([^<]+)</p>"
                    r".*?href=\"(https://worldbankgroup\.csod\.com/[^\"]*requisition/(\d+)[^\"]*)\"",
                    re.S)
                for wm in _wb_rx.finditer(body):
                    title, loc = wm.group(1).strip(), wm.group(2).strip()
                    url, rid = wm.group(3).split("?")[0], wm.group(4)
                    if rid in seen or not title or len(title) < 5:
                        continue
                    if broad and not ROLE_RE.search(title):
                        continue
                    seen.add(rid)
                    yield {"source": label, "org": "World Bank", "ext_id": rid,
                           "title": title[:130], "loc": loc, "deadline": "",
                           "url": url, "scan": title.lower()}
                continue
            for lm in link_rx.finditer(body):
                gs = lm.groups()
                url = html.unescape(gs[0]).split("?")[0]  # strip utm params
                title = html.unescape(gs[-1]).strip()      # title is always last group
                eid = gs[2] if len(gs) >= 4 else (gs[1] if len(gs) >= 3 else url)
                emp_slug = gs[1] if len(gs) >= 4 else ""
                if eid in seen or not title or len(title) < 5:
                    continue
                if broad and not ROLE_RE.search(title):
                    continue     # broad alerts: keep only economist-relevant roles
                seen.add(eid)
                org = emp_slug.replace("-", " ").title() if emp_slug else label
                yield {"source": label, "org": org, "ext_id": eid, "title": title[:130],
                       "loc": "", "deadline": "", "url": url, "scan": title.lower()}
    try:
        M.close(); M.logout()
    except Exception:
        pass


def cleanup_alert_inbox():
    """Archive processed alert emails from inbox (Gmail: delete from INBOX = archive)."""
    creds = load_creds()
    addr = creds.get("email.address", "")
    pw = creds.get("email.app_password", "").replace(" ", "")
    if not addr or not pw:
        return 0
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=CTX)
        M.login(addr, pw)
        M.select("INBOX")
    except Exception:
        return 0
    since = (datetime.now() - timedelta(days=IMAP_DAYS)).strftime("%d-%b-%Y")
    archived = 0
    all_patterns = [fp for _, fp, _, _ in ALERT_SENDERS] + ARCHIVE_EXTRA
    for from_pat in all_patterns:
        try:
            _, nums = M.search(None, f'(FROM "{from_pat}" SINCE {since})')
        except Exception:
            continue
        for num in (nums[0] or b"").split():
            try:
                M.store(num, "+FLAGS", "(\\Seen \\Deleted)")
                archived += 1
            except Exception:
                pass
    if archived:
        try:
            M.expunge()
        except Exception:
            pass
    try:
        M.close(); M.logout()
    except Exception:
        pass
    return archived


# ── per-job classification into a category ──────────────────────────────────
_CENTRAL_BANKS = re.compile(
    r"federal reserve|reserve bank|central bank|banque de france|banque centrale|bundesbank"
    r"|riksbank|norges bank|bank of finland|banco de espa[nñ]a|bank of england|bank of japan"
    r"|bank of canada|bank of korea|swiss national bank|national bank of belgium|nbb|hkma"
    r"|monetary authority|Danmarks Nationalbank|Danmarks NB|banco central|banca d.italia"
    r"|de nederlandsche bank|oesterreichische nationalbank|bank of israel|bank of greece"
    r"|central bank of turkey|türkiye cumhuriyet merkez bankası", re.I)

_MULTILATERAL_BANKS = {"EBRD", "CEB", "AIIB", "ADB", "BSTDB", "BIS", "IDB", "NDB", "World Bank", "IMF", "OECD"}
_MULTILATERAL_RX = re.compile(
    r"\bEBRD\b|\bCEB\b|\bAIIB\b|\bADB\b|Asian Development Bank|\bBSTDB\b|\bBIS\b"
    r"|\bIDB\b|Inter-American Development Bank|\bNDB\b|New Development Bank"
    r"|World Bank|\bIMF\b|International Monetary Fund|\bOECD\b", re.I)

_IO_UN = re.compile(
    r"UNjobs|united nations|^UN\b|UNICEF|UNDP|UNIDO|UNESCO|UNCTAD|UNHCR|WFP|FAO|WHO|ILO"
    r"|IPCC|WTO|CEPAL|ECLAC|IAEA|IOM|UNEP|UNFPA", re.I)

def classify(it):
    org = it.get("org", "")
    src = it.get("source", "")
    ejm = it.get("ejm_types", [])
    # EJM position types take priority for non-academic roles
    if ejm:
        ejm_lo = " ".join(ejm).lower()
        if "postdoc" in ejm_lo:
            return "Academic"
        if any(k in ejm_lo for k in ("assistant prof", "full prof", "associate prof", "lecturer")):
            return "Academic"
    # Central banks — check org name first
    if _CENTRAL_BANKS.search(org):
        return "Central banks"
    if org in ("HKMA", "Bank of Finland", "Banco de Espana", "NBB Belgium", "Danmarks NB", "NBB",
               "Banque de France"):
        return "Central banks"
    # Multilateral banks — exact match OR regex on full org string (catches "IDB - Inter-American Development Bank")
    if org in _MULTILATERAL_BANKS or _MULTILATERAL_RX.search(org):
        return "Multilateral"
    # Academic institutions — check before IO/UN so "European University Institute" doesn't become IO
    if re.search(r"universit|college|school of|faculty of", org, re.I) and src not in ("ReliefWeb",):
        return "Academic"
    # IOs / UN system — but not if org is a multilateral bank
    if _IO_UN.search(org) or src in ("ReliefWeb", "Impactpool", "UNjobs"):
        return "IO/UN"
    if org in ("WFP", "IPCC"):
        return "IO/UN"
    # Think tanks & research
    if org in ("J-PAL", "UNU-WIDER", "IPA", "WZB", "CREI", "UNU", "CMCC", "CGD",
               "ifo Institute", "Bruegel"):
        return "Think tanks & research"
    if org in ("U St Gallen (SIAW)",):
        return "Academic"
    # Consulting
    if org in ("Ecorys", "Compass Lexecon", "Cornerstone Research", "Frontier Economics",
               "London Economics", "Cambridge Econometrics", "RBB Economics",
               "E.CA Economics", "Copenhagen Economics", "NERA", "CRA", "Brattle"):
        return "Consulting"
    # Private sector
    if org in ("Lombard Odier", "Mirabaud", "Trafigura", "Wood Mackenzie",
               "Aurora Energy", "Rystad Energy", "Moody's", "Bank J. Safra Sarasin"):
        return "Private sector"
    # Title-based fallback
    t = it["title"].lower()
    if re.search(r"professor|lecturer|faculty|postdoc|post-doc|ph\.?d|doctoral|tenure|research fellow|readership|assistant prof", t):
        return "Academic"
    if _CENTRAL_BANKS.search(t):
        return "Central banks"
    if re.search(r"consult|economic consulting", t):
        return "Consulting"
    if re.search(r"capital|asset manage|investment|trading|insurance|hedge fund|private equity", t):
        return "Private sector"
    if re.search(r"institute|foundation|think.?tank", t):
        return "Think tanks & research"
    if re.search(r"economic affairs|policy analyst|policy officer", t):
        return "IO/UN"
    return "Academic"   # aggregators are academia-heavy by default


CAT_ORDER = ["Central banks", "Policy / IO", "Think tanks & research", "Private sector", "Academic", "Other"]
ACAD_SUB_ORDER = ["Assistant Professor", "Postdoc", "Research Fellow", "Lecturer", "Other academic"]
PRIVATE_SUB_ORDER = ["Swiss finance", "Global banks & asset mgrs", "Commodities & energy",
                     "Insurance & rating agencies", "Economic consulting", "Mgmt consulting & tech",
                     "Other private sector"]
SUB_ORDER = ACAD_SUB_ORDER + PRIVATE_SUB_ORDER + ["Multilateral banks", "IOs/UN/NGOs/Govts"]

# Employer → private-sector sub-category. Uses the same buckets as Marcelo's Source Tracker.
# ponytail: hardcoded because adapter org labels don't line up with sources.yaml source names (CRA vs
# "Charles River Associates (CRA)", etc.); a few dozen strings is clearer than a fuzzy-match table.
PRIVATE_SUB = {
    "Swiss finance": ["Lombard Odier", "Mirabaud", "Pictet", "UBS", "UBP", "Julius Baer", "Vontobel",
                      "Bank J. Safra Sarasin", "GAM Investments", "Swiss Re", "Zurich Insurance"],
    "Global banks & asset mgrs": ["J.P. Morgan", "Goldman Sachs", "Morgan Stanley", "Bank of America",
                                  "Citi", "Barclays", "Deutsche Bank", "BNP Paribas", "HSBC", "Nomura",
                                  "Standard Chartered", "BlackRock", "PIMCO", "Amundi", "Schroders",
                                  "AQR Capital", "Bridgewater", "Man Group"],
    "Commodities & energy": ["Trafigura", "Vitol", "Mercuria", "Gunvor", "Shell", "BP", "TotalEnergies",
                             "Wood Mackenzie", "Aurora Energy", "Rystad Energy"],
    "Insurance & rating agencies": ["Allianz Research", "AXA", "Munich Re", "Moody's Analytics",
                                    "S&P Global", "Fitch"],
    "Economic consulting": ["Compass Lexecon", "Cornerstone Research", "Frontier Economics",
                            "London Economics", "Cambridge Econometrics", "RBB Economics",
                            "E.CA Economics", "Copenhagen Economics", "NERA", "CRA", "Ecorys",
                            "Swiss Economics", "Analysis Group", "The Brattle Group", "Oxera"],
    "Mgmt consulting & tech": ["McKinsey", "BCG", "Oliver Wyman", "PwC", "EY", "Amazon", "Google",
                               "Microsoft", "Meta", "Uber", "Booking.com", "Visa", "Mastercard",
                               "Deloitte"],
}
_ORG_SUB = {org: sub for sub, orgs in PRIVATE_SUB.items() for org in orgs}


def _private_sub(org):
    if org in _ORG_SUB:
        return _ORG_SUB[org]
    lo = (org or "").lower()
    for known, sub in _ORG_SUB.items():
        if known.lower() in lo:   # "McKinsey Global Institute" starts with "McKinsey"
            return sub
    return ""


def job_type(it):
    """classify() category → (category, sub-category) for the dashboard's two-level job-type filter."""
    cat = it["cat"]
    if cat == "Academic":
        return cat, academic_sub(it["title"])
    if cat in ("Consulting", "Private sector"):   # both sit under Private sector
        return "Private sector", _private_sub(it["org"]) or (
            "Economic consulting" if cat == "Consulting" else "Other private sector")
    if cat == "Multilateral":
        return "Policy / IO", "Multilateral banks"
    if cat == "IO/UN":
        return "Policy / IO", "IOs/UN/NGOs/Govts"
    if cat == "Policy / IO":
        return "Policy / IO", ""
    if cat == "Policy / IO (UN system)":
        return "Policy / IO", "IOs/UN/NGOs/Govts"
    return cat, ""


def academic_sub(title):
    t = title.lower()
    if re.search(r"post-?doc|postdoctoral", t): return "Postdoc"
    if re.search(r"assistant professor|tenure-track|\bw1\b|junior professor", t): return "Assistant Professor"
    if re.search(r"research fellow|research associate|senior researcher|research scientist", t): return "Research Fellow"
    if re.search(r"lecturer", t): return "Lecturer"
    return "Other academic"


def parse_sources():
    """sources.yaml → [{cat, sub, name, url, note}] for the nested directory."""
    out, cat, sub = [], "Other", ""
    if not SRCS.exists():
        return out
    for ln in SRCS.read_text(encoding="utf-8").splitlines():
        s = ln.strip()
        if s.startswith("#"):
            txt = re.sub(r"\s*\(.*?\)", "", s.lstrip("#")).strip()   # "(top location tiers)" etc. is commentary
            if (txt and "═" not in txt and "─" not in txt and 2 < len(txt) < 80
                    and not re.match(r"(type|url|selector|credentials|note|status|how|for |annotation|rss|html|manual|\[|\(|http|ponytail|--)", txt, re.I)):
                if "—" in txt:
                    cat, sub = [x.strip() for x in txt.split("—", 1)]
                else:
                    cat, sub = txt, ""
            continue
        m = re.search(r'name:\s*"([^"]+)".*?url:\s*"([^"]+)"(?:.*?note:\s*"([^"]*)")?', s)
        if m:
            st, how = re.search(r"status:\s*(\w+)", s), re.search(r'how:\s*"([^"]*)"', s)
            out.append({"cat": cat, "sub": sub, "name": m.group(1), "url": m.group(2), "note": m.group(3) or "",
                        "status": st.group(1) if st else "blocked",   # unclassified = nothing pulls it yet
                        "how": how.group(1) if how else ""})
    return out


# ── dashboard: tracking status per source ───────────────────────────────────
# Labels of the hand-written adapters wired in main() — keep in sync. The Workday / SmartRecruiters /
# GENERIC_SITES / ALERT_SENDERS / OPEN_BOARDS labels are read from their own lists.
AGGREGATORS = ["UNjobs", "jobs.ac.uk", "Inomics", "Euraxess", "Impactpool", "ReliefWeb", "CAGI"]
PAGE_ADAPTERS = ["EBRD", "CEB", "AIIB", "ADB", "BSTDB", "BIS", "HKMA", "J-PAL", "UNU-WIDER", "Bank of Finland",
                 "CEPAL", "WZB", "Ecorys", "CRA", "CREI", "SIAW", "WTO", "UNU", "CMCC",
                 "Brattle", "Moodys", "CGD", "Safra", "ifo"]
# adapter label → sources.yaml name, only where they differ after dropping "(…)" and accents.
# ponytail: a new adapter whose label matches no entry shows up under "Other tracked feeds" — add it here.
YAML_NAME = {
    "Danmarks NB": "Danmarks Nationalbank", "ReliefWeb": "ReliefWeb Jobs", "HKMA": "Hong Kong Monetary Authority",
    "CEPAL": "CEPAL / ECLAC", "WZB": "WZB Berlin Social Science Center", "CRA": "Charles River Associates",
    "SIAW": "University of St. Gallen", "DIW": "DIW Berlin", "ZEW": "ZEW Mannheim",
    "Grantham/LSE": "Grantham Research Institute", "NERA": "NERA Economic Consulting",
    "Aurora Energy": "Aurora Energy Research", "KOF/ETH": "KOF Swiss Economic Institute",
    "UZH Econ": "University of Zurich", "ETH jobs": "ETH Zurich", "TSE": "Toulouse School of Economics",
    "IHEID": "IHEID open positions", "IPA": "Innovations for Poverty Action", "NBB Belgium": "National Bank of Belgium",
    "NBB": "National Bank of Belgium", "IDB": "IDB / BID", "unvacancies": "UN Hiring Index", "World Bank": "World Bank Group",
    "UNU": "United Nations University", "CMCC": "CMCC (climate)",
    "Brattle": "The Brattle Group", "Moodys": "Moody's Analytics", "CGD": "CGD (DC / London)",
    "Safra": "Bank J. Safra Sarasin", "ifo": "ifo Institute (Munich)",
}


def _fold(s):
    """'Banco de España' → 'banco de espana': drop accents and punctuation."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _name(s):
    return _fold(re.sub(r"\(.*?\)", " ", s))   # 'CREI (Barcelona)' matches label 'CREI'


def tracked_feeds():
    """Every feed the watcher pulls: label → how it's pulled."""
    feeds = {}
    for how, labels in [("Workday feed", [w[0] for w in WORKDAY]), ("SmartRecruiters feed", [s[0] for s in SMARTRECRUITERS]),
                        ("aggregator", AGGREGATORS), ("careers page", PAGE_ADAPTERS + [g[0] for g in GENERIC_SITES]),
                        ("email alert", [a[0] for a in ALERT_SENDERS]), ("job board", [b[1] for b in OPEN_BOARDS])]:
        for label in labels:
            feeds.setdefault(label, []).append(how)
    return {label: " + ".join(dict.fromkeys(h)) for label, h in feeds.items()}


def _feeds_by_name():
    """sources.yaml name → the watcher's feed labels for it."""
    by_name = {}
    for label in tracked_feeds():
        by_name.setdefault(_name(YAML_NAME.get(label, label)), []).append(label)
    return by_name


def source_rows(directory, jobs):
    """sources.yaml entries (status comes from the Source Tracker) + roles found this run, for feeds the watcher pulls."""
    by_name, found = _feeds_by_name(), Counter(j["source"] for j in jobs)
    return [{**e, "n": sum(found[l] for l in by_name[_name(e["name"])]) if _name(e["name"]) in by_name else None}
            for e in directory]


def tracking_drift(directory):
    """Where sources.yaml's status and the watcher's actual feeds disagree (printed by --dry-run)."""
    by_name = _feeds_by_name()
    names = {_name(e["name"]) for e in directory}
    msgs = [f"{e['name']}: status {e['status']}, but " + ("the watcher has no feed for it" if e["status"] == "directly"
                                                          else "the watcher pulls it")
            for e in directory
            if (e["status"] == "directly") != (_name(e["name"]) in by_name) and e["status"] != "alert"]
    return msgs + [f"{', '.join(v)}: watcher feed with no sources.yaml entry" for k, v in by_name.items() if k not in names]


# ── dashboard: free-text location → (region, country) ───────────────────────
REGIONS = {
    "Switzerland": "Switzerland",
    "Western Europe": "France, Germany, United Kingdom, Ireland, Belgium, Netherlands, Luxembourg, Austria, Italy, Spain, "
                      "Portugal, Denmark, Sweden, Norway, Finland, Iceland, Malta, Monaco, Liechtenstein",
    "Rest of Europe": "Poland, Czechia, Hungary, Slovakia, Slovenia, Croatia, Romania, Bulgaria, Greece, Cyprus, Estonia, "
                      "Latvia, Lithuania, Serbia, Ukraine, Moldova, Albania, North Macedonia, Montenegro, Kosovo, "
                      "Bosnia and Herzegovina, Turkey, Armenia, Azerbaijan, Belarus",
    "North America": "United States, Canada",
    "Latin America": "Mexico, Brazil, Chile, Argentina, Colombia, Peru, Uruguay, Paraguay, Bolivia, Ecuador, Venezuela, "
                     "Panama, Costa Rica, Guatemala, Honduras, El Salvador, Nicaragua, Dominican Republic, Haiti, Jamaica, "
                     "Barbados, Trinidad and Tobago, Guyana, Suriname, Belize, Cuba",
    "Asia-Pacific": "China, Hong Kong, Macau, Taiwan, Japan, South Korea, Mongolia, Singapore, Malaysia, Indonesia, "
                    "Philippines, Thailand, Vietnam, Cambodia, Laos, Myanmar, India, Pakistan, Bangladesh, Sri Lanka, "
                    "Nepal, Bhutan, Maldives, Afghanistan, Kazakhstan, Kyrgyzstan, Uzbekistan, Tajikistan, Turkmenistan, "
                    "Australia, New Zealand, Fiji, Samoa, Papua New Guinea, Solomon Islands, Timor-Leste",
    "Middle East & Africa": "Saudi Arabia, United Arab Emirates, Qatar, Kuwait, Bahrain, Oman, Yemen, Israel, Palestine, "
                            "Jordan, Lebanon, Syria, Iraq, Iran, Egypt, Libya, Tunisia, Algeria, Morocco, Mauritania, Sudan, "
                            "South Sudan, Eritrea, Ethiopia, Djibouti, Somalia, Kenya, Uganda, Rwanda, Burundi, Tanzania, "
                            "Mozambique, Malawi, Zambia, Zimbabwe, Botswana, Namibia, South Africa, Lesotho, Eswatini, "
                            "Madagascar, Mauritius, Seychelles, Comoros, Angola, DR Congo, Congo, Gabon, Equatorial Guinea, "
                            "Sao Tome and Principe, Cameroon, Chad, Central African Republic, Nigeria, Niger, Ghana, Togo, "
                            "Benin, Burkina Faso, Mali, Senegal, Gambia, Guinea-Bissau, Guinea, Sierra Leone, Liberia, "
                            "Cote d'Ivoire, Cabo Verde",
}
ALIASES = {"uk": "United Kingdom", "england": "United Kingdom", "scotland": "United Kingdom", "wales": "United Kingdom",
           "usa": "United States", "united states of america": "United States", "uae": "United Arab Emirates",
           "swiss": "Switzerland", "korea": "South Korea", "turkiye": "Turkey", "czech republic": "Czechia",
           "ivory coast": "Cote d'Ivoire", "democratic republic of the congo": "DR Congo", "cape verde": "Cabo Verde",
           "macao": "Macau", "viet nam": "Vietnam"}
# trailing ", XX" country codes (Inomics: "Gaithersburg, US"); codes that are also US states (CA, IL, IN, CO…) left out
ISO = dict(p.split(":") for p in (
    "CH:Switzerland,FR:France,DE:Germany,GB:United Kingdom,UK:United Kingdom,IE:Ireland,BE:Belgium,NL:Netherlands,"
    "LU:Luxembourg,AT:Austria,IT:Italy,ES:Spain,PT:Portugal,DK:Denmark,SE:Sweden,NO:Norway,FI:Finland,PL:Poland,"
    "CZ:Czechia,HU:Hungary,SK:Slovakia,SI:Slovenia,HR:Croatia,RO:Romania,BG:Bulgaria,GR:Greece,CY:Cyprus,EE:Estonia,"
    "LV:Latvia,LT:Lithuania,US:United States,MX:Mexico,BR:Brazil,CL:Chile,PE:Peru,CN:China,HK:Hong Kong,JP:Japan,"
    "KR:South Korea,SG:Singapore,AU:Australia,NZ:New Zealand,AE:United Arab Emirates,SA:Saudi Arabia,QA:Qatar,"
    "ZA:South Africa,KE:Kenya").split(","))
# cities / institutions seen in postings → country. Only consulted when no country name appears.
# ponytail: keyword lookup, leftmost hit wins; unknown places fall to "Not specified" — add the keyword here.
PLACES = {
    "Switzerland": "geneva, geneve, genf, zurich, basel, bern, berne, lausanne, st gallen, lucerne, luzern, fribourg, "
                   "neuchatel, lugano, zug, winterthur, gerzensee, epfl, eth",
    "France": "paris, toulouse, lyon, marseille, strasbourg, lille, bordeaux, grenoble",
    "Germany": "berlin, frankfurt, munich, munchen, mannheim, kiel, potsdam, bonn, cologne, koln, hamburg, heidelberg, "
               "halle, essen, dresden, leipzig, stuttgart, tubingen, freiburg, gottingen, konstanz, mainz, munster, "
               "dusseldorf, bamberg, bayreuth, nuremberg, regensburg, jena, bremen, dortmund, bochum",
    "United Kingdom": "london, oxford, cambridge, edinburgh, manchester, warwick, glasgow, bristol, birmingham, leeds, "
                      "nottingham, sheffield, exeter, durham, st andrews, southampton, sussex, essex, belfast, cardiff, "
                      "lancaster, liverpool, newcastle, aberdeen, surrey, norwich, leicester, coventry, brighton, york",
    "Ireland": "dublin", "Belgium": "brussels, bruxelles, leuven, louvain, ghent, antwerp",
    "Netherlands": "amsterdam, rotterdam, the hague, utrecht, tilburg, maastricht, groningen, leiden, nijmegen, delft, wageningen",
    "Austria": "vienna, wien, graz, innsbruck, linz, salzburg, laxenburg",
    "Italy": "rome, milan, florence, bologna, turin, naples, venice, padua, pisa, trento, siena",
    "Spain": "madrid, barcelona, valencia, seville, bilbao", "Portugal": "lisbon, porto",
    "Denmark": "copenhagen, aarhus, roskilde", "Sweden": "stockholm, uppsala, lund, gothenburg", "Norway": "oslo, bergen",
    "Finland": "helsinki", "Iceland": "reykjavik", "Poland": "warsaw, krakow", "Czechia": "prague", "Hungary": "budapest",
    "Slovakia": "bratislava", "Slovenia": "ljubljana", "Croatia": "zagreb", "Romania": "bucharest", "Bulgaria": "sofia",
    "Greece": "athens, thessaloniki", "Cyprus": "nicosia", "Estonia": "tallinn", "Latvia": "riga", "Lithuania": "vilnius",
    "Serbia": "belgrade", "Montenegro": "podgorica", "Kosovo": "pristina", "Bosnia and Herzegovina": "sarajevo",
    "North Macedonia": "skopje", "Albania": "tirana", "Moldova": "chisinau", "Ukraine": "kyiv, kiev",
    "Armenia": "yerevan", "Azerbaijan": "baku", "Turkey": "istanbul, ankara",
    "United States": "washington, dc, new york, boston, chicago, san francisco, philadelphia, atlanta, dallas, cleveland, "
                     "st louis, kansas city, richmond, minneapolis, los angeles, san diego, seattle, princeton, stanford, "
                     "berkeley, new haven, ann arbor, baltimore, bethesda, gaithersburg, pittsburgh, houston, austin, "
                     "denver, miami, youngstown",
    "Canada": "ottawa, toronto, montreal, vancouver", "Mexico": "mexico city", "Brazil": "sao paulo, rio de janeiro, brasilia",
    "Chile": "santiago", "Argentina": "buenos aires", "Colombia": "bogota", "Peru": "lima", "Uruguay": "montevideo",
    "Paraguay": "asuncion", "Bolivia": "la paz", "Ecuador": "quito", "Venezuela": "caracas", "Honduras": "tegucigalpa",
    "Nicaragua": "managua", "Dominican Republic": "santo domingo", "Haiti": "port au prince",
    "China": "beijing, shanghai, shenzhen", "Macau": "taipa", "Japan": "tokyo", "South Korea": "seoul",
    "Mongolia": "ulaanbaatar", "Philippines": "manila, asian development bank", "India": "new delhi, delhi, mumbai",
    "Pakistan": "islamabad", "Bangladesh": "dhaka", "Sri Lanka": "colombo", "Nepal": "kathmandu", "Afghanistan": "kabul",
    "Myanmar": "yangon", "Cambodia": "phnom penh", "Laos": "vientiane", "Vietnam": "hanoi, ho chi minh",
    "Thailand": "bangkok", "Indonesia": "jakarta", "Malaysia": "kuala lumpur", "Kazakhstan": "astana, almaty",
    "Kyrgyzstan": "bishkek", "Uzbekistan": "tashkent", "Tajikistan": "dushanbe",
    "Australia": "sydney, melbourne, canberra", "New Zealand": "wellington, auckland", "Fiji": "suva",
    "United Arab Emirates": "abu dhabi, dubai", "Saudi Arabia": "riyadh, jeddah", "Qatar": "doha", "Kuwait": "kuwait city",
    "Oman": "muscat", "Yemen": "sanaa, aden", "Iraq": "baghdad, erbil", "Syria": "damascus", "Lebanon": "beirut",
    "Jordan": "amman", "Palestine": "gaza, ramallah, west bank", "Israel": "tel aviv, jerusalem", "Egypt": "cairo",
    "Tunisia": "tunis", "Morocco": "rabat", "Algeria": "algiers", "Libya": "tripoli", "Sudan": "khartoum",
    "South Sudan": "juba", "Ethiopia": "addis ababa", "Somalia": "mogadishu", "Kenya": "nairobi", "Uganda": "kampala",
    "Rwanda": "kigali", "Burundi": "bujumbura", "Tanzania": "dar es salaam, dodoma", "Mozambique": "maputo",
    "Malawi": "lilongwe", "Zambia": "lusaka", "Zimbabwe": "harare", "Botswana": "gaborone", "Namibia": "windhoek",
    "South Africa": "johannesburg, pretoria, cape town", "Madagascar": "antananarivo", "Angola": "luanda",
    "DR Congo": "kinshasa", "Congo": "brazzaville", "Gabon": "libreville", "Cameroon": "yaounde, douala",
    "Chad": "ndjamena", "Nigeria": "lagos, abuja", "Niger": "niamey", "Ghana": "accra", "Togo": "lome",
    "Benin": "cotonou", "Burkina Faso": "ouagadougou", "Mali": "bamako", "Senegal": "dakar", "Gambia": "banjul",
    "Guinea": "conakry", "Sierra Leone": "freetown", "Liberia": "monrovia", "Cote d'Ivoire": "abidjan",
    "Cabo Verde": "praia", "Sao Tome and Principe": "sao tome",
}
REGION_OF = {c: r for r, cs in REGIONS.items() for c in cs.split(", ")}


def _lookup(pairs):
    keys = dict(pairs)
    return keys, re.compile(r"\b(" + "|".join(sorted(map(re.escape, keys), key=len, reverse=True)) + r")\b")


_COUNTRIES = _lookup([(_fold(c), c) for c in REGION_OF] + [(k, c) for k, c in ALIASES.items()])
_CITIES = _lookup([(_fold(k), c) for c, ks in PLACES.items() for k in ks.split(", ")])
_MULTI_RE = re.compile(r"remote|home.?based|various|multiple|several|\d+\s+locations|worldwide|global", re.I)


def _country(text):
    t = _fold(text)
    for keys, rx in (_COUNTRIES, _CITIES):   # "Cambridge, MA, United States" → the country name wins
        m = rx.search(t)
        if m:
            return keys[m.group(1)]
    m = re.search(r",\s*([A-Z]{2})\s*$", text or "")
    return ISO.get(m.group(1), "") if m else ""


def place(it):
    """(region, country) for a role: location first, then employer name, then title."""
    loc = it.get("loc") or ""
    c = _country(loc) or ("" if _MULTI_RE.search(loc) else _country(it.get("org")) or _country(it["title"]))
    if c:
        return REGION_OF[c], c
    return ("Remote & multiple" if _MULTI_RE.search(loc) else "Not specified"), ""


REGION_ORDER = list(REGIONS) + ["Remote & multiple", "Not specified"]   # Marcelo's location tiers: CH > W-Europe > …
DASH_TPL = BASE / "dashboard_template.html"
DASH_OUT = BASE / "dashboard.html"
WORLDMAP = BASE / "worldmap.svg"   # generated once by gen_map.py from world-atlas countries-110m


def iso_date(s):
    """Deadline text → 'YYYY-MM-DD' ('' if unreadable). Seen: 2026-10-15, 2026/09/20, 12/30/2026, 09 Oct 2026, Oct 08, 2026."""
    s = re.sub(r"(?<=[A-Za-z])\.", "", re.sub(r"\s+", " ", (s or "").strip()))   # "Oct. 8" → "Oct 8"
    m = re.match(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
    elif m := re.fullmatch(r"(\d{1,2})([/.-])(\d{1,2})\2(\d{4})", s):
        a, sep, b, y = int(m.group(1)), m.group(2), int(m.group(3)), int(m.group(4))
        # ponytail: dotted dates are European (15.10.2026); an ambiguous a/b/yyyy is read as Workday's US month/day
        d, mo = (a, b) if sep == "." or a > 12 else (b, a)
    else:
        for fmt in ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y"):
            try:
                return datetime.strptime(s, fmt).date().isoformat()
            except ValueError:
                pass
        return ""
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return ""


_GENDER = re.compile(r"^[mfwdxh](\s*/\s*[mfwdxh])+$", re.I)   # (m/f/d), (H/F): drop
_INSTITUTION = re.compile(r"\b(institute|institut|universit\w*|college|school|faculty|department|centre|center|bank|fund|"
                          r"office|division|authority|agency|commission|ministry|council|foundation|academy)\b", re.I)
BOARD_SOURCES = set(AGGREGATORS) | {a[0] for a in ALERT_SENDERS} | {b[1] for b in OPEN_BOARDS} | {"EJM", "JOE"}   # list other employers' jobs


def split_title(title):
    """'Assistant Manager (HK Institute for …) (AoF vacancy)' → ('Assistant Manager', ['HK Institute for …', 'AoF vacancy'])."""
    parts = []
    while (m := re.search(r"\s+[(\[]([^()\[\]]*)[)\]]?\s*$", title)) and m.start() >= 3:   # also an unclosed "(…" cut at 130 chars
        title = title[:m.start()]
        if m.group(1).strip() and not _GENDER.match(m.group(1).strip()):
            parts.insert(0, m.group(1).strip())
    return title.strip(" -–,"), parts


def card(j, country):
    """The fields a job card shows: clean title, employer (shown with 'via source'), location, extra details."""
    title, parts = split_title(j["title"])
    loc = j["loc"] or country
    if j["loc"] and title.lower().endswith(", " + j["loc"].lower()):   # UNjobs: "ECONOMIST, Libreville"
        title = title[:-len(j["loc"]) - 2]
    if title.isupper():
        title = " ".join(w.capitalize() for w in title.split())
    employer = "" if j["org"] in (j["source"], j["loc"]) or j["org"].startswith("UNjobs") else j["org"]
    if not employer:   # an institution named in the title beats the careers-page owner (HKMA → HKIMR)
        employer = next((p for p in parts if _INSTITUTION.search(p) and len(p.split()) > 1), "")
        if employer:
            parts.remove(employer)
        elif j["source"] not in BOARD_SOURCES:
            employer = j["source"]
    details, seen = [], {employer.lower(), loc.lower()}
    for d in parts + j.get("extra", []):
        if d and d.lower() not in seen:
            seen.add(d.lower())
            details.append(d)
    return title or j["title"], employer, loc, details


def write_dashboard(jobs, directory):
    """Bake roles + sources into the template → one self-contained HTML page (no server, no fetch)."""
    safe = lambda u: u if u.startswith(("https://", "http://")) else ""   # scraped hrefs are untrusted
    roles = []
    for j in jobs:
        region, country = place(j)
        title, employer, loc, details = card(j, country)
        roles.append({"title": title, "url": safe(j["url"]), "employer": employer, "source": j["source"], "loc": loc,
                      "details": details, "deadline": j["deadline"], "due": iso_date(j["deadline"]), "cat": j["cat"],
                      "sub": j.get("sub", ""), "region": region, "country": country, "new": j["new"]})
    sources = [{**s, "url": safe(s["url"])} for s in source_rows(directory, jobs)]
    data = {"stamp": f"{datetime.now():%d %b %Y · %H:%M}", "catOrder": CAT_ORDER, "subOrder": SUB_ORDER,
            "regionOrder": REGION_ORDER, "roles": roles, "sources": sources}
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")   # can't close the <script> early
    head = ('<!doctype html>\n<html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n')
    world = WORLDMAP.read_text(encoding="utf-8") if WORLDMAP.exists() else '<p class="hint">Map data missing.</p>'
    page = DASH_TPL.read_text(encoding="utf-8").replace("__WORLDMAP__", world).replace("__DATA__", blob)
    DASH_OUT.write_text(head + page, encoding="utf-8")
    return DASH_OUT


def db_connect():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS seen(source TEXT, ext_id TEXT, title TEXT, url TEXT, first_seen TEXT, "
              "PRIMARY KEY(source, ext_id))")
    return c


def is_seen(c, s, e):
    return c.execute("SELECT 1 FROM seen WHERE source=? AND ext_id=?", (s, e)).fetchone() is not None


class _Tee:
    def __init__(self, *streams): self.streams = streams
    def write(self, s):
        for x in self.streams:
            try: x.write(s)
            except Exception: pass
    def flush(self):
        for x in self.streams:
            try: x.flush()
            except Exception: pass


def load_creds():
    d = {}
    if CREDS.exists():
        sec = None
        for ln in CREDS.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln.startswith("[") and ln.endswith("]"):
                sec = ln[1:-1]
            elif "=" in ln and not ln.startswith("#"):
                k, v = ln.split("=", 1)
                d[f"{sec}.{k.strip()}"] = v.split("#")[0].strip()
    return d


def send_email(creds, subject, text_body, html_body, attachment=None, attachment_name=""):
    addr = creds.get("email.address", ""); pw = creds.get("email.app_password", "").replace(" ", "")
    to = creds.get("email.to", "") or addr
    if not addr or not pw:
        return False
    msg = MIMEMultipart("mixed"); msg["Subject"], msg["From"], msg["To"] = subject, addr, to
    body = MIMEMultipart("alternative")
    body.attach(MIMEText(text_body, "plain", "utf-8")); body.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(body)
    if attachment:
        part = MIMEText(attachment.read_text(encoding="utf-8"), "html", "utf-8")
        part.add_header("Content-Disposition", "attachment", filename=attachment_name or attachment.name)
        msg.attach(part)
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as srv:
        srv.starttls(context=CTX); srv.login(addr, pw); srv.sendmail(addr, [to], msg.as_string())
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    # ponytail: Task Scheduler swallows stdout, so tee to watcher.log — one line per run tells you if send failed.
    if not args.dry_run:
        log = open(BASE / "watcher.log", "a", encoding="utf-8", buffering=1)
        log.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} argv={sys.argv[1:]} ===\n")
        _stdout, _stderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = _Tee(_stdout, log)

    conn = db_connect()   # dry-run reads NEW flags too; only a sent email (or --init) commits
    items = (list(adapter_workday()) + list(adapter_smartrecruiters()) + list(adapter_unjobs())
             + list(adapter_jobsacuk()) + list(adapter_inomics()) + list(adapter_euraxess())
             + list(adapter_ebrd()) + list(adapter_ceb()) + list(adapter_aiib())
             + list(adapter_adb()) + list(adapter_bstdb())
             + list(adapter_bis()) + list(adapter_hkma())
             + list(adapter_jpal()) + list(adapter_unu_wider())
             + list(adapter_bof()) + list(adapter_cepal())
             + list(adapter_wzb()) + list(adapter_ecorys())
             + list(adapter_cra()) + list(adapter_crei()) + list(adapter_siaw())
             + list(adapter_unu_careers()) + list(adapter_cmcc())
             + list(adapter_brattle()) + list(adapter_moodys())
             + list(adapter_cgd()) + list(adapter_safra()) + list(adapter_ifo())
             + list(adapter_wto_page()) + list(adapter_generic())
             + list(adapter_impactpool()) + list(adapter_reliefweb())
             + list(adapter_cagi()) + list(adapter_imap())
             + list(adapter_open_boards())
             + list(adapter_ejm()) + list(adapter_joe()))

    # ── cross-source fuzzy dedup: merge same role from different aggregators ──
    def _norm_title(t):
        t = re.sub(r"\[.*?\]", "", t)   # strip [National Staff], [Open to Tier...]
        t = re.sub(r"\(.*?\)", "", t)    # strip (2 positions) etc.
        t = re.sub(r"\b[PGD]-?\d\b", "", t, flags=re.I)   # strip UN grades P4, G5, D1
        t = re.sub(r"\bNO-?[A-D]\b", "", t, flags=re.I)   # strip NO-A, NO-B etc.
        return re.sub(r"[^a-z]+", " ", t.lower()).strip()

    def _norm_loc(raw):
        """Normalize location to a set of words for fuzzy matching."""
        return set(re.sub(r"[^a-z]+", " ", raw.lower()).strip().split()) if raw else set()

    def _loc_key(words):
        return " ".join(sorted(words))

    deduped = []
    seen_keys = {}      # norm_title+loc_key → index in deduped
    seen_by_title = {}  # norm_title → [(loc_words, index)] for fuzzy loc match

    def _find_dup(nt, loc_words, org_words):
        """Find existing entry with same title and overlapping location."""
        if f"{nt}|{_loc_key(loc_words)}" in seen_keys:
            return seen_keys[f"{nt}|{_loc_key(loc_words)}"]
        # fuzzy: same title, both have locations, locations share a word, AND orgs overlap
        if loc_words and org_words:
            for prev_words, idx in seen_by_title.get(nt, []):
                if prev_words and loc_words & prev_words:
                    prev_org = _norm_loc(deduped[idx].get("org") or "")
                    if prev_org & org_words:
                        return idx
        return None

    for it in items:
        nt = _norm_title(it["title"])
        loc_words = _norm_loc(it.get("loc") or "")
        org_words = _norm_loc(it.get("org") or "")
        dup_idx = _find_dup(nt, loc_words, org_words)
        if dup_idx is not None:
            kept = deduped[dup_idx]
            for f in ("loc", "deadline", "org"):
                if not kept.get(f) and it.get(f):
                    kept[f] = it[f]
            if len(it["title"]) > len(kept["title"]):
                kept["title"] = it["title"]
            continue
        lk = _loc_key(loc_words)
        seen_keys[f"{nt}|{lk}"] = len(deduped)
        seen_by_title.setdefault(nt, []).append((loc_words, len(deduped)))
        deduped.append(it)

    now = datetime.now().isoformat(timespec="seconds")
    pending, jobs = [], []
    for it in deduped:
        if GERMAN_REQ.search(it.get("scan", "")):
            continue
        if INTERN_EXCLUDE.search(it["title"]):
            continue     # drop internships
        it["cat"] = classify(it)
        if it["cat"] == "Academic" and ACAD_EXCLUDE.search(it["title"]):
            continue     # drop assoc/full professor and PhD/doctoral-student roles
        it["new"] = not (conn is not None and is_seen(conn, it["source"], it["ext_id"]))
        it["cat"], it["sub"] = job_type(it)
        pending.append((it["source"], it["ext_id"], it["title"], it["url"], now))
        jobs.append(it)

    def commit_seen():
        if conn is not None:
            conn.executemany("INSERT OR IGNORE INTO seen VALUES(?,?,?,?,?)", pending); conn.commit()

    directory = parse_sources()
    date = f"{datetime.now():%Y-%m-%d}"
    n_new = sum(1 for j in jobs if j["new"])

    if args.init:
        commit_seen(); print(f"Baseline: {len(jobs)} roles marked seen."); return

    dash = write_dashboard(jobs, directory)
    if args.dry_run:
        print(f"Parsed {len(jobs)} roles ({n_new} new); directory = {len(directory)} sources.")
        print("by source:", dict(sorted(Counter(j["source"] for j in jobs).items())))
        print("by category:", dict(sorted(Counter(j["cat"] for j in jobs).items())))
        print("status drift (sources.yaml vs watcher feeds):", *(tracking_drift(directory) or ["none"]), sep="\n  ")
        print(f"Dashboard: {dash}")
        return

    # Email = short note + the dashboard attached (its filters need a browser, not a mail client).
    new = sorted((j for j in jobs if j["new"]), key=lambda j: (j["cat"], j["title"].lower()))
    who = lambda j: ", ".join(x for x in (j["org"], j["loc"]) if x)
    li = "".join(f"<li style='margin:0 0 5px'><a href='{html.escape(j['url'])}'>{html.escape(j['title'])}</a>"
                 f" <span style='color:#888'>— {html.escape(who(j))}</span></li>" for j in new)
    html_body = (f"<div style='font-family:Georgia,serif;font-size:15px;max-width:640px'>"
                 f"<p><b>{len(jobs)} open roles</b>, {n_new} new since the last run.</p>"
                 f"<p>Open the attached <b>Econ Job Finder</b> in your browser to filter by job type and location."
                 f" The latest copy is always at {html.escape(str(dash))}.</p>"
                 + (f"<p style='margin:18px 0 6px'><b>New since the last run</b></p><ul style='padding-left:18px'>{li}</ul>" if new else "")
                 + "</div>")
    text_body = "\n".join([f"{len(jobs)} open roles, {n_new} new since the last run.",
                           f"Dashboard attached; latest copy at {dash}", ""]
                          + [f"- {j['title']} | {who(j)}\n  {j['url']}" for j in new])
    subject = f"[Job watch] {len(jobs)} roles ({n_new} new) — {date}"
    creds = load_creds()
    try:
        sent = send_email(creds, subject, text_body, html_body, dash, f"econ-job-finder-{date}.html")
    except Exception as e:
        print(f"Email failed: {e}"); sent = False
    if sent:
        commit_seen()
        print(f"Emailed dashboard: {len(jobs)} roles ({n_new} new), {len(directory)} sources.")
    else:
        print(f"Not sent (check [email] creds). Dashboard written: {dash}")
    # Archive alerts unconditionally: once adapter_imap has read them, their jobs are already in the
    # dashboard file — keeping them in INBOX just clogs it while nothing new can be extracted next run.
    try:
        archived = cleanup_alert_inbox()
        if archived:
            print(f"Archived {archived} alert emails from INBOX.")
    except Exception as e:
        print(f"Cleanup failed: {e}")


if __name__ == "__main__":
    main()
