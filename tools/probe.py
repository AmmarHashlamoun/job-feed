"""Manual probe for building new board readers (run by .github/workflows/probe.yml).

PROBE_URLS: one request per line: "URL" or "POST URL {json}". Prints status, final URL,
size, interesting links and a short body sample.
PROBE_ROWS: companies.csv-style lines (company,ats,token,extra,pathway,tier). Runs each
through the real feed code and prints seen/kept counts, errors and sample titles.
Nothing is written to the repo.
"""
import csv
import io
import json
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scraper"))
import feed  # noqa: E402

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
LINK_RX = re.compile(r"https?://[^\s\"'<>]*(?:job|career|talent|getro|consider|ashby|greenhouse|lever|workday|"
                     r"oraclecloud|successfactors|icims|smartrecruiters|pinpoint|teamtailor)[^\s\"'<>]*", re.I)


def fetch(line):
    dump, size = None, 3500
    if " #dump=" in line:
        line, dump = line.split(" #dump=", 1)
        size = 3500
        if " #n=" in dump:
            dump, n = dump.split(" #n=", 1)
            size = int(n)
    parts = line.split(" ", 2)
    method, url, body = ("GET", line, None)
    if parts[0] in ("POST", "GET") and len(parts) >= 2:
        method, url = parts[0], parts[1]
        body = parts[2].encode() if len(parts) > 2 else None
    h = {"User-Agent": BROWSER_UA, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
         "Accept-Language": "en-CA,en;q=0.9"}
    if body:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    print(f"\n=== {method} {url}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
            print(f"status {r.status} final {r.geturl()} bytes {len(raw)}")
    except Exception as e:
        code = getattr(e, "code", "")
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")[:600]  # type: ignore[attr-defined]
        except Exception:
            pass
        print(f"ERROR {code} {type(e).__name__}: {str(e)[:200]} {raw[:300]}")
        return
    if dump:
        for m in list(re.finditer(dump, raw))[:2]:
            print(f"  DUMP {dump!r} at {m.start()}:")
            print(raw[m.start():m.start() + size])
        return
    links = list(dict.fromkeys(LINK_RX.findall(raw)))[:25]
    for l in links:
        print("  link:", l[:200])
    for pat in [r'"network":\{"id":"?\d+', r"AF_initDataCallback\(\{key: 'ds:\d+'", r"__staticRouterHydrationData",
                r'"totalRecords"\s*:\s*\d+', r'"count"\s*:\s*\d+', r'"total"\s*:\s*\d+', r"window\.__\w+",
                r'"jobs"\s*:\s*\[', r'"items"\s*:\s*\[', r'data-job-id', r'jobTitle', r'"title"\s*:']:
        m = re.search(pat, raw)
        if m:
            print(f"  has {pat!r} at {m.start()}: {raw[m.start():m.start()+160]!r}")
    print("  sample:", re.sub(r"\s+", " ", raw[:700]))


def run_rows(text):
    for r in csv.DictReader(io.StringIO("company,ats,token,extra,pathway,tier\n" + text)):
        r = {k: (v or "").strip() for k, v in r.items()}
        print(f"\n=== ROW {r}")
        try:
            raw = feed.ADAPTERS[r["ats"]](r["token"], r["extra"]) if r["ats"] in feed.ADAPTERS else None
            if raw is not None:
                print(f"  raw seen {len(raw)}")
                for j in raw[:8]:
                    print("   raw:", j.get("title"), "|", j.get("locations"), "|", j.get("url"))
        except Exception as e:
            print("  raw ERROR", type(e).__name__, str(e)[:300])
        rec, kept = feed.run_board(r)
        print("  rec:", json.dumps(rec))
        for j in kept[:10]:
            print("   kept:", j["title"], "|", j["location"][:80], "|", j["url"])


if __name__ == "__main__":
    for line in (os.environ.get("PROBE_URLS") or "").splitlines():
        if line.strip():
            fetch(line.strip())
    rows = (os.environ.get("PROBE_ROWS") or "").strip()
    if rows:
        run_rows(rows)
