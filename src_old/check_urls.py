import re, sys, urllib.request, urllib.error, ssl, socket
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path(__file__).resolve().parent
SRC = str(BASE / "sources.yaml")
OUT = str(BASE / "url_check_report.md")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CTX = ssl.create_default_context()

line_re = re.compile(r'name:\s*"([^"]+)".*?url:\s*"([^"]+)"(?:.*?type:\s*(\w+))?')

entries = []
for ln in open(SRC, encoding="utf-8"):
    m = line_re.search(ln)
    if m:
        entries.append((m.group(1), m.group(2), m.group(3) or "html"))

def fetch(url, method):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA, "Accept": "*/*"})
    return urllib.request.urlopen(req, timeout=15, context=CTX)

def check(e):
    name, url, typ = e
    try:
        r = fetch(url, "GET")   # GET only: HEAD gives false 404s on many servers
        return (name, url, typ, r.status, r.geturl())
    except urllib.error.HTTPError as h:
        return (name, url, typ, h.code, "")
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, ConnectionError) as ex:
        reason = getattr(ex, "reason", ex)
        return (name, url, typ, f"ERR:{reason}", "")
    except Exception as ex:
        return (name, url, typ, f"ERR:{ex}", "")

with ThreadPoolExecutor(max_workers=16) as ex:
    results = list(ex.map(check, entries))

def bucket(code):
    if isinstance(code, int):
        if 200 <= code < 300: return "OK"
        if 300 <= code < 400: return "REDIRECT"
        if code in (401, 403, 406, 429): return "BLOCKED"
        if code in (404, 410): return "DEAD"
        return "HTTP_ERR"
    return "ERROR"

from urllib.parse import urlsplit
def probe_root(url):
    s = urlsplit(url); root = f"{s.scheme}://{s.netloc}/"
    try:
        return root, fetch(root, "GET").status
    except urllib.error.HTTPError as h:
        return root, h.code
    except Exception:
        return root, "ERR"

need = [r for r in results if bucket(r[3]) in ("DEAD", "HTTP_ERR", "ERROR")]
roots = {}
with ThreadPoolExecutor(max_workers=16) as ex:
    for r, res in zip(need, ex.map(lambda x: probe_root(x[1]), need)):
        roots[r[1]] = res

groups = {}
for name, url, typ, code, final in results:
    b = bucket(code)
    groups.setdefault(b, []).append((name, url, typ, code, final))

order = ["DEAD", "HTTP_ERR", "ERROR", "BLOCKED", "REDIRECT", "OK"]
labels = {
    "DEAD": "DEAD — 404/410, fix these",
    "HTTP_ERR": "HTTP ERROR — 4xx/5xx, check",
    "ERROR": "CONNECTION ERROR — timeout/DNS/SSL, check manually (often fine)",
    "BLOCKED": "BLOCKED — 401/403/406/429 bot-block, URL likely fine, open in browser",
    "REDIRECT": "REDIRECT — landed elsewhere, consider updating URL",
    "OK": "OK — 2xx",
}

lines = ["# URL check report — sources.yaml", ""]
lines.append(f"Total: {len(results)} | " + " | ".join(f"{b}:{len(groups.get(b,[]))}" for b in order))
lines.append("")
for b in order:
    rows = groups.get(b, [])
    if not rows: continue
    lines.append(f"## {labels[b]}  ({len(rows)})")
    lines.append("")
    fallback = b in ("DEAD", "HTTP_ERR", "ERROR")
    if fallback:
        lines.append("| Name | Status | Broken URL | Root domain (fallback) | Root status |")
        lines.append("|---|---|---|---|---|")
    else:
        lines.append("| Name | Status | URL | Redirected to |")
        lines.append("|---|---|---|---|")
    for name, url, typ, code, final in sorted(rows, key=lambda x: x[0].lower()):
        if fallback:
            ru, rc = roots.get(url, ("", ""))
            lines.append(f"| {name} | {code} | {url} | {ru} | {rc} |")
        else:
            red = final if (final and final.rstrip('/') != url.rstrip('/')) else ""
            lines.append(f"| {name} | {code} | {url} | {red} |")
    lines.append("")

open(OUT, "w", encoding="utf-8").write("\n".join(lines))
print("Total:", len(results))
for b in order:
    print(f"  {b}: {len(groups.get(b,[]))}")
print("\nDEAD (fix):")
for name, url, typ, code, final in sorted(groups.get("DEAD", []), key=lambda x: x[0].lower()):
    print(f"  - {name}: {url}")
print("\nREDIRECT (maybe update):")
for name, url, typ, code, final in sorted(groups.get("REDIRECT", []), key=lambda x: x[0].lower()):
    print(f"  - {name}: {url} -> {final}")
print("\nReport written to", OUT)
