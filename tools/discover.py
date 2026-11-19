"""Find which job board (ATS) a company uses, starting from its careers page.

Input: tools/probe-input/discover.txt, one company per line: "Company | url [url ...]".
For each company it fetches the pages (and up to 8 career-looking links on them, one level deep),
pulls out every ATS board it can recognise, then runs the boards the feed can read through the real
feed code (scraper/feed.py) to report how many postings it sees and keeps.
Output: tools/probe-output/discover.json (+ a short text summary on stdout).
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scraper"))
import feed  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

PATTERNS = [
    ("workday", re.compile(r"([a-z0-9][a-z0-9-]*)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_\-]+)")),
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:-api)?\.greenhouse\.io/(?:v1/boards/|embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_\-]+)")),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_\-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_\-\.]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_\-\.%]+)")),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_\-]+)")),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_\-]+)")),
    ("bamboohr", re.compile(r"([a-z0-9\-]+)\.bamboohr\.com")),
    ("recruitee", re.compile(r"([a-z0-9\-]+)\.recruitee\.com")),
    ("oraclehcm", re.compile(r"([a-z0-9\-\.]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[a-z\-]+/sites/([A-Za-z0-9_]+)")),
    ("ukg", re.compile(r"recruiting2?\.ultipro\.(?:com|ca)/([A-Z0-9]+)/JobBoard/([0-9a-f\-]{36})")),
    ("icims", re.compile(r"((?:careers|jobs)-?[a-z0-9\-]*\.icims\.com)")),
    ("taleo", re.compile(r"([a-z0-9\-]+\.taleo\.net)/careersection/([A-Za-z0-9_\-]+)")),
    ("successfactors", re.compile(r"(career\d*\.successfactors\.(?:com|eu))/career\?[^\"'\s<>]*company=([A-Za-z0-9_\-]+)")),
    ("rmk", re.compile(r"(?:href|src)=[\"'](https?://[a-z0-9\.\-]+)/(?:search/\?|job/[^\"']+/\d+/|go/[^\"']+/\d+/)")),
    ("dayforce", re.compile(r"jobs\.dayforcehcm\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_\-]+)/([A-Za-z0-9_\-]+)")),
    ("avature", re.compile(r"([a-z0-9\-]+\.avature\.net)")),
    ("njoyn", re.compile(r"([a-z0-9\-]+\.njoyn\.com)")),
    ("jobvite", re.compile(r"jobs\.jobvite\.com/([A-Za-z0-9_\-]+)")),
    ("eightfold", re.compile(r"([a-z0-9\-]+\.eightfold\.ai)")),
    ("phenom", re.compile(r"(cdn\.phenompeople\.com|phenom)")),
    ("paylocity", re.compile(r"recruiting\.paylocity\.com/[^\"'\s<>]*")),
    ("adp", re.compile(r"(workforcenow\.adp\.com/[^\"'\s<>]*|myjobs\.adp\.com/[^\"'\s<>]*)")),
    ("pinpoint", re.compile(r"([a-z0-9\-]+\.pinpointhq\.com)")),
    ("teamtailor", re.compile(r"([a-z0-9\-]+\.teamtailor\.com)")),
    ("breezy", re.compile(r"([a-z0-9\-]+\.breezy\.hr)")),
    ("jazzhr", re.compile(r"([a-z0-9\-]+\.applytojob\.com)")),
    ("rippling", re.compile(r"ats\.rippling\.com/([A-Za-z0-9_\-]+)")),
    ("hibob", re.compile(r"([a-z0-9\-]+\.careers\.hibob\.com)")),
    ("brassring", re.compile(r"(sjobs\.brassring\.com/[^\"'\s<>]*)")),
    ("cornerstone", re.compile(r"([a-z0-9\-]+\.csod\.com)")),
    ("peopleadmin", re.compile(r"([a-z0-9\-]+\.peopleadmin\.com)")),
]
CAREER_LINK = re.compile(r"href=[\"']([^\"'#]+)[\"']", re.I)
CAREER_WORD = re.compile(r"career|job|opening|opportunit|join|hiring|position|vacanc|emploi|search", re.I)
SKIP_WORD = re.compile(r"\.(pdf|jpg|png|svg|css|js)(\?|$)|linkedin|facebook|twitter|instagram|youtube|glassdoor|indeed", re.I)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
                                               "Accept-Language": "en-CA,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.geturl(), r.read(2_500_000).decode("utf-8", "replace")


def find_boards(text, final_url=""):
    found = []
    blob = text + " " + final_url
    for kind, rx in PATTERNS:
        for m in rx.finditer(blob):
            g = m.groups() if m.groups() else (m.group(0),)
            if kind == "greenhouse" and g[0].lower() in ("embed", "v1", "js", "jobs"):
                continue
            if kind == "ashby" and g[0].lower() in ("api",):
                continue
            found.append((kind,) + tuple(g))
    return list(dict.fromkeys(found))


def test_board(company, b):
    kind = b[0]
    row = None
    if kind == "workday":
        row = {"company": company, "ats": "workday", "token": b[1], "extra": f"{b[2]}|{b[3]}"}
    elif kind in ("greenhouse", "lever", "ashby", "smartrecruiters", "workable", "bamboohr", "recruitee"):
        row = {"company": company, "ats": kind, "token": urllib.parse.unquote(b[1]), "extra": ""}
    elif kind == "oraclehcm":
        row = {"company": company, "ats": "oraclehcm", "token": b[1], "extra": b[2]}
    elif kind == "ukg":
        row = {"company": company, "ats": "ukg", "token": b[1], "extra": b[2]}
    elif kind in feed.ADAPTERS and kind in ("rmk", "dayforce", "icims", "taleo", "successfactors", "njoyn", "avature"):
        row = {"company": company, "ats": kind, "token": b[1], "extra": b[2] if len(b) > 2 else ""}
    if not row:
        return None
    row.update({"pathway": "E", "tier": "3"})
    rec, kept = feed.run_board(row)
    return {"row": row, "ok": rec["ok"], "seen": rec["seen"], "kept": rec["kept"], "error": rec["error"],
            "sample": [f'{j["title"]} | {j["location"][:60]}' for j in kept[:6]]}


def discover(line):
    company, _, urls = line.partition("|")
    company = company.strip()
    urls = urls.split()
    out = {"company": company, "pages": [], "boards": [], "tests": []}
    queue, seen_pages = list(urls), set()
    depth_links = []
    for u in queue:
        try:
            final, text = get(u)
        except Exception as e:
            out["pages"].append({"url": u, "error": f"{getattr(e, 'code', '')} {type(e).__name__}"})
            continue
        seen_pages.add(u)
        out["pages"].append({"url": u, "final": final, "bytes": len(text)})
        out["boards"] += find_boards(text, final)
        base = final
        for href in CAREER_LINK.findall(text):
            full = urllib.parse.urljoin(base, href)
            if not full.startswith("http") or SKIP_WORD.search(full):
                continue
            if CAREER_WORD.search(full) and full not in seen_pages:
                depth_links.append(full)
    for u in list(dict.fromkeys(depth_links))[:8]:
        try:
            final, text = get(u)
        except Exception as e:
            out["pages"].append({"url": u, "error": f"{getattr(e, 'code', '')} {type(e).__name__}"})
            continue
        out["pages"].append({"url": u, "final": final, "bytes": len(text)})
        out["boards"] += find_boards(text, final)
    out["boards"] = [list(b) for b in dict.fromkeys(tuple(b) for b in out["boards"])]
    tested = set()
    for b in out["boards"]:
        key = tuple(b[:3])
        if key in tested:
            continue
        tested.add(key)
        try:
            t = test_board(company, tuple(b))
        except Exception as e:
            t = {"row": list(b), "ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"}
        if t:
            out["tests"].append(t)
        if len(tested) >= 6:
            break
    return out


def main():
    path = os.path.join(ROOT, "tools", "probe-input", "discover.txt")
    lines = [l.strip() for l in open(path, encoding="utf-8") if l.strip() and not l.startswith("#")]
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(discover, lines))
    os.makedirs(os.path.join(ROOT, "tools", "probe-output"), exist_ok=True)
    json.dump(results, open(os.path.join(ROOT, "tools", "probe-output", "discover.json"), "w"), indent=1)
    for r in results:
        best = max(r["tests"], key=lambda t: t.get("seen", 0), default=None)
        kinds = sorted({b[0] for b in r["boards"]})
        print(f'{r["company"]}: boards={kinds} best={best["row"] if best else None} '
              f'seen={best.get("seen") if best else 0} kept={best.get("kept") if best else 0}')


if __name__ == "__main__":
    main()
