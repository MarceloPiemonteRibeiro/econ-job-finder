# Econ Job Finder

A daily dashboard of open economics roles for the 2026-27 job market — curated,
classified, and filterable.

- **Live site:** https://marcelopiemonteribeiro.github.io/econ-job-finder/
- **Refreshes:** every weekday at 08:00 CET
- **Author:** [Marcelo Piemonte Ribeiro](https://github.com/MarceloPiemonteRibeiro) (IHEID PhD, JM 2026-27)

## What it does

Every morning `watcher.py` scrapes ~65 employer career pages, aggregators, and
job boards for economics roles, plus reads job-alert emails from Gmail. It
classifies each role into a job type (Central banks, Policy/IO, Think tanks,
Private sector, Academic) and a location (region → country), then bakes the
whole thing into a self-contained HTML page. `publish.py` pushes that page here
as `index.html`; GitHub Pages serves it.

The dashboard has three tabs:

- **Roles** — every open role filterable by job type, location, deadline, and
  free-text search. Roles closing today, this week, this month, or later.
- **Map** — world map coloured by open roles per country. Click a country to
  filter the Roles tab.
- **Sources** — every tracked source (277 as of Sept 2026) with its status:
  scraped directly, read from email alerts, or blocked by JS/paywall.

## Sources tracked

`sources.yaml` holds the full directory (see it in this repo). Roles come from:

- **Direct scraping** (~66 sources): IMF, OECD, EBRD, ADB, AIIB, Banque de
  France, WTO, BIS, HKMA, Bank of Finland, J-PAL, UNU-WIDER, jobs.ac.uk,
  Inomics, Euraxess, UNjobs, Impactpool, ReliefWeb, OpenPostdoc,
  OpenFacultyJobs, plus think tanks (Bruegel, PIIE, ZEW, Kiel, DIW, KOF, RFF,
  WRI, PIK, Grantham/LSE) and consulting firms (Compass Lexecon, Cornerstone,
  Frontier, NERA, RBB, CRA, Copenhagen Economics, and more).
- **Email alerts** (~16 sources): Academic Positions, IDB, World Bank, NDB,
  ITU, Fitch, WFP, Mastercard, NIB, EIB, UNIL, Pictet, EU Careers, 80,000
  Hours, Idealist, HSLU, University of Bern.
- **Manual**: JOE (AEA) and EJM — checked by hand from their own portals.

## Repo contents

```
econ-job-finder/
├── index.html                 the live dashboard (rebuilt daily)
├── src/
│   ├── watcher.py             the scraper + email alert reader + dashboard writer
│   ├── publish.py             pushes index.html here
│   ├── dashboard_template.html HTML/CSS/JS template with placeholder data
│   ├── sources.yaml           the full source directory (277 entries)
│   └── worldmap.svg           embedded world map (Natural Earth 110m)
├── README.md
└── .gitignore
```

Local-only files not in this repo: `credentials.local` (Gmail app password,
GitHub token), `seen.db` (sqlite dedup store), `watcher.log` (run log).

## Running it yourself

The scraper is stdlib-only Python 3.14 (no `pip install`). To fork and run:

```
git clone https://github.com/<you>/econ-job-finder.git
cd econ-job-finder
# Create credentials.local (see credentials.local.example)
python src/watcher.py --init      # baseline seen.db without emailing
python src/watcher.py --dry-run   # write dashboard.html, don't send email
```

## Credit and licence

Code: MIT. Dashboard content: role listings belong to their respective
employers and job boards. Please don't scrape this dashboard itself — go to
the sources directly.
