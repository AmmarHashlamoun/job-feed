#!/usr/bin/env python3
"""Daily job feed for Ammar's new-grad search.

Reads the public job boards listed in companies.csv (Greenhouse, Lever, Ashby,
Workday, SmartRecruiters, Workable, plus Amazon, Microsoft, Uber and Eightfold
career APIs), keeps software roles in Toronto/GTA, Hamilton, Niagara, Waterloo
region or remote Canada that aren't internships or senior roles, and writes:

  feed/feed.json    every kept posting (one object per posting)
  feed/health.json  per-board result: ok, seen, kept, error
  feed/seen.json    first_seen date per posting id (keeps first_seen stable)

Standard library only. Run: python3 scraper/feed.py
"""
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "feed")
UA = "job-feed/1.0 (+https://github.com/AmmarHashlamoun/job-feed)"
TIMEOUT = 25

# ---------------------------------------------------------------- filters
AREA_STRICT = (r"toronto|north york|scarborough|etobicoke|mississauga|markham|vaughan|brampton|oakville|"
               r"guelph|richmond hill|oshawa|pickering|whitby|st\.? catharines|niagara|kitchener|waterloo|"
               r"thornhill|stoney creek|grimsby|welland")
AREA_NEEDS_PROVINCE = r"hamilton|burlington|cambridge|milton|aurora|newmarket|concord|woodbridge|bolton|ajax|king city"
RX_STRICT = re.compile(rf"\b({AREA_STRICT})\b", re.I)
RX_PROV = re.compile(rf"\b({AREA_NEEDS_PROVINCE})\b[ ,\-]*(on\b|ont\b|ontario|canada|can\b)", re.I)
RX_ONTARIO = re.compile(r"\bontario\b|,\s*on\b(?!\w)|\bON,\s*(CA|CAN|Canada)\b", re.I)
RX_OTTAWA_ONLY = re.compile(r"ottawa|kanata|nepean|gatineau", re.I)
RX_CANADA = re.compile(r"\bcanada\b|\bCAN\b|\bCA-ON\b", re.I)
RX_REMOTE = re.compile(r"remote|anywhere|work from home|wfh|virtual|distributed", re.I)
RX_OTHER_CA = re.compile(r"vancouver|burnaby|victoria|montr[eé]al|qu[eé]bec|calgary|edmonton|halifax|winnipeg|"
                         r"saskatoon|regina|fredericton|st\.? john|surrey|richmond, bc|ottawa|kanata|gatineau|laval", re.I)

EXCLUDE_TITLE = re.compile(
    r"\bintern\b|internship|co-?op\b|\bco op\b|student|apprentice|summer 20\d\d|fall 20\d\d|winter 20\d\d|"
    r"\bsenior\b|\bsr\.?\b|\bstaff\b|principal|\blead\b|\bleader\b|manager|director|head of|\bvp\b|vice president|"
    r"architect|distinguished|\bfellow\b|\b(iii|iv|v)\b|\bphd\b|post-?doc|"
    r"mechanical|civil\b|structural|chemical|technician|recruit|account executive|account manager|"
    r"sales engineer|sales\b|marketing|counsel|attorney|accountant|payroll|\bhr\b|human resources|"
    r"customer success manager|support specialist|help ?desk|it support|desktop support|"
    r"quality assurance analyst|qa analyst|manual test|support engineer|technical services|maintenance engineer|"
    r"stagiaire|\bstage\b|systems engineer(?!.*software)", re.I)
SOFTWARE_TITLE = re.compile(
    r"software|developer|programmer|\bswe\b|\bsde\b|machine learning|\bml\b|\bai\b|data (engineer|scien)|"
    r"devops|\bsre\b|site reliability|firmware|embedded|robotic|autonom|perception|computer vision|"
    r"back[- ]?end|front[- ]?end|full[- ]?stack|mobile|\bios\b|android|\bplatform\b|infrastructure|cloud|"
    r"security engineer|forward deployed|deployed engineer|deployment engineer|solutions engineer|"
    r"applications? engineer|implementation (engineer|specialist|consultant)|integration engineer|"
    r"customer engineer|field (application )?engineer|technical solutions|compiler|kernel|graphics|"
    r"research engineer|automation engineer|test automation|windows engineer|networking|distributed systems|"
    r"web (developer|engineer)|\bllm\b|agent", re.I)
HARDWARE_ONLY = re.compile(r"(electrical|hardware|asic|fpga|rf|analog|layout|verification|validation|process|"
                           r"manufacturing|test|reliability|quality|mechanical|optical|photonic) engineer", re.I)
JUNIOR = re.compile(r"new grad|graduate|early career|entry[- ]level|junior|\bjr\b|associate|university|campus|"
                    r"recent grad|\b(i|1)\b\s*$|\b(engineer|developer|analyst) (i|1)\b|engineer in training|\beit\b", re.I)
MID = re.compile(r"\b(ii|2)\b", re.I)


def location_verdict(texts):
    """Return (keep, remote) for a list of location strings."""
    joined = " | ".join(t for t in texts if t)
    if not joined.strip():
        return False, False
    if RX_STRICT.search(joined) or RX_PROV.search(joined):
        return True, bool(RX_REMOTE.search(joined))
    if RX_ONTARIO.search(joined) and not RX_OTTAWA_ONLY.search(joined):
        return True, bool(RX_REMOTE.search(joined))
    if RX_CANADA.search(joined):
        parts = [p.strip() for p in re.split(r"\||;|/| or ", joined) if p.strip()]
        for p in parts:
            bare = re.sub(r"[^a-z& ]", " ", p.lower()).split()
            bare_ok = " ".join(bare) in ("canada", "can", "ca", "united states and canada", "us and canada",
                                         "usa and canada", "us & canada", "north america")
            if RX_CANADA.search(p) and (RX_REMOTE.search(p) or bare_ok):
                return True, True
    return False, False


def title_verdict(title):
    """Return (keep, level_hint, reason)."""
    if EXCLUDE_TITLE.search(title):
        return False, "", "title_excluded"
    if not SOFTWARE_TITLE.search(title):
        return False, "", "not_software"
    if HARDWARE_ONLY.search(title) and not re.search(r"software|firmware|embedded|ml|machine learning|automation", title, re.I):
        return False, "", "not_software"
    if JUNIOR.search(title):
        return True, "junior", ""
    if MID.search(title):
        return True, "mid", ""
    return True, "unlevelled", ""


# ---------------------------------------------------------------- http
def http(url, data=None, headers=None, method=None):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h, method=method or ("POST" if body else "GET"))
    last = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
                return json.loads(raw.decode("utf-8", "replace")) if raw else None
        except urllib.error.HTTPError as e:
            if e.code in (404, 410, 400, 401, 403):
                raise
            last = e
        except Exception as e:  # timeouts, resets, bad JSON
            last = e
        time.sleep(2)
    raise last


def iso(v):
    if v in (None, ""):
        return ""
    if isinstance(v, (int, float)):
        return dt.datetime.utcfromtimestamp(v / 1000 if v > 1e11 else v).strftime("%Y-%m-%d")
    return str(v)[:10]


YEARS_RX = re.compile(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*(\d{1,2})\s*)?\+?\s*(?:years?|yrs?)"
                      r"(?:\s+of)?(?:\s+[\w/&,-]+){0,6}?\s+(?:experience|exp\b)", re.I)
GRAD_RX = re.compile(r"[^.\n]{0,140}\b(graduat\w*|degree (?:by|before|prior)|enrolled|currently pursuing|class of 20\d\d|"
                     r"completed (?:a|your) (?:bachelor|degree))\b[^.\n]{0,140}", re.I)


def strip_html(s):
    import html as _h
    s = _h.unescape(_h.unescape(s or ""))
    s = re.sub(r"<(br|/p|/li|/div|/h\d)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[ \t\r\f\v]+", " ", s)


def extract_quals(text):
    """Return (min_years or None, years snippet, graduation snippet) from a job description."""
    text = strip_html(text)
    mins, snippet = [], ""
    for m in YEARS_RX.finditer(text):
        n = int(m.group(1))
        if 0 <= n <= 15:
            mins.append(n)
            if not snippet:
                snippet = text[max(0, m.start() - 40):m.end() + 20].strip().replace("\n", " ")
    g = GRAD_RX.search(text)
    return (min(mins) if mins else None), snippet[:160], (g.group(0).strip().replace("\n", " ")[:220] if g else "")


# Ammar's skills, matching config.md: used for real / learning (counts half). Anything else is a gap.
HAS = ["python", "java", "typescript", "javascript", "sql", "html", "css", "react", "next.js", "tailwind",
       "supabase", "postgresql", "nlp", "spacy", "hugging face", "transformers", "bert", "pandas", "git",
       "linux", "bash", "node.js", "rest", "api"]
LEARNING = ["c++", "ros", "ros 2"]
TECH = {
    "python": r"\bpython\b", "java": r"\bjava\b(?!script)", "typescript": r"typescript|\bts\b", "javascript": r"javascript|\bjs\b",
    "sql": r"\bsql\b", "html": r"\bhtml", "css": r"\bcss\b", "react": r"\breact\b", "next.js": r"next\.?js",
    "tailwind": r"tailwind", "supabase": r"supabase", "postgresql": r"postgres", "node.js": r"node\.?js|\bnode\b",
    "nlp": r"\bnlp\b|natural language", "spacy": r"spacy", "hugging face": r"hugging ?face", "transformers": r"transformer",
    "bert": r"\bbert\b", "pandas": r"pandas", "git": r"\bgit\b", "linux": r"linux|unix", "bash": r"\bbash\b|shell script",
    "rest": r"\brest(ful)?\b", "api": r"\bapis?\b",
    "c++": r"c\+\+|\bcpp\b", "ros": r"\bros\b|ros ?2", "c": r"\bc\b(?![+#])|embedded c", "c#": r"c#|\.net\b",
    "go": r"\bgolang\b|\bgo\b(?= |,|/)", "rust": r"\brust\b", "kotlin": r"kotlin", "swift": r"\bswift\b",
    "scala": r"\bscala\b", "ruby": r"\bruby\b|rails", "php": r"\bphp\b",
    "aws": r"\baws\b|amazon web services", "gcp": r"\bgcp\b|google cloud", "azure": r"\bazure\b",
    "docker": r"docker|container", "kubernetes": r"kubernetes|\bk8s\b", "terraform": r"terraform",
    "ci/cd": r"ci/cd|jenkins|github actions|gitlab ci", "kafka": r"\bkafka\b", "spark": r"\bspark\b",
    "pytorch": r"pytorch", "tensorflow": r"tensorflow", "jax": r"\bjax\b", "cuda": r"\bcuda\b", "llm": r"\bllms?\b",
    "rtos": r"\brtos\b", "fpga": r"\bfpga\b", "verilog": r"verilog|systemverilog", "embedded": r"embedded|firmware|microcontroller",
    "android": r"\bandroid\b", "ios": r"\bios\b", "graphql": r"graphql", "mongodb": r"mongo",
    "distributed systems": r"distributed system", "compilers": r"compiler|llvm|mlir",
}
SECTION_RX = re.compile(r"(minimum|basic|required|requirements|qualifications|what you.?ll need|you have|you bring|"
                        r"must have|who you are|about you|preferred|nice to have|bonus)", re.I)


def extract_stack(text):
    """Return (stack, has, learning, missing, req_excerpt) from a job description."""
    text = strip_html(text)
    low = text.lower()
    stack = [k for k, rx in TECH.items() if re.search(rx, low)]
    has = [k for k in stack if k in HAS]
    learning = [k for k in stack if k in LEARNING]
    missing = [k for k in stack if k not in HAS and k not in LEARNING]
    m = SECTION_RX.search(text)
    start = m.start() if m else 0
    excerpt = re.sub(r"\s*\n\s*", " | ", text[start:start + 900]).strip(" |")
    return stack, has, learning, missing, excerpt[:900]


DETAIL = {
    "greenhouse": lambda d: (d or {}).get("content", ""),
    "workday": lambda d: ((d or {}).get("jobPostingInfo") or {}).get("jobDescription", ""),
    "pcsx": lambda d: (((d or {}).get("data") or d or {}).get("jobDescription") or ""),
    "smartrecruiters": lambda d: " ".join(((s or {}).get("text") or "") for s in (((d or {}).get("jobAd") or {}).get("sections") or {}).values()),
}


# ---------------------------------------------------------------- adapters
# Each returns a list of raw postings: {id, title, locations[list], url, posted}

def greenhouse(token, _):
    d = http(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
    out = []
    for j in d.get("jobs", []):
        locs = [(j.get("location") or {}).get("name", "")]
        for o in j.get("offices", []) or []:
            locs.append(o.get("location") or o.get("name") or "")
        out.append({"id": j["id"], "title": j.get("title", ""), "locations": locs,
                    "url": j.get("absolute_url", ""), "posted": iso(j.get("updated_at")),
                    "detail": ("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{j['id']}")})
    return out


def lever(token, _):
    d = http(f"https://api.lever.co/v0/postings/{token}?mode=json")
    if isinstance(d, dict):
        raise RuntimeError(d.get("message", "unexpected response"))
    out = []
    for j in d:
        c = j.get("categories") or {}
        locs = [c.get("location", "")] + list(c.get("allLocations") or [])
        if j.get("workplaceType") == "remote":
            locs = [l + " (remote)" for l in locs if l] or ["Remote"]
        if j.get("country"):
            locs.append(j["country"] if j["country"] != "CA" else "Canada")
        desc = " ".join([j.get("descriptionPlain") or ""] + [(x.get("text") or "") + " " + (x.get("content") or "") for x in (j.get("lists") or [])] + [j.get("additionalPlain") or ""])
        out.append({"id": j["id"], "title": j.get("text", ""), "locations": locs,
                    "url": j.get("hostedUrl", ""), "posted": iso(j.get("createdAt")), "desc": desc})
    return out


def ashby(token, _):
    d = http(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    out = []
    for j in d.get("jobs", []):
        locs = [j.get("location", "")]
        for s in j.get("secondaryLocations", []) or []:
            locs.append(s.get("location", ""))
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        if addr:
            locs.append(", ".join(x for x in [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")] if x))
        if j.get("isRemote"):
            locs = [l + " (remote)" for l in locs if l]
        out.append({"id": j.get("id") or j.get("jobUrl"), "title": j.get("title", ""), "locations": locs,
                    "url": j.get("jobUrl", ""), "posted": iso(j.get("publishedAt")), "desc": j.get("descriptionPlain") or j.get("descriptionHtml") or ""})
    return out


def smartrecruiters(token, _):
    out, offset = [], 0
    while True:
        d = http(f"https://api.smartrecruiters.com/v1/companies/{token}/postings?country=ca&limit=100&offset={offset}")
        for j in d.get("content", []):
            l = j.get("location") or {}
            loc = ", ".join(x for x in [l.get("city"), l.get("region"), "Canada" if (l.get("country") or "").lower() == "ca" else l.get("country")] if x)
            if l.get("remote"):
                loc += " (remote)"
            out.append({"id": j["id"], "title": j.get("name", ""), "locations": [loc],
                        "url": f"https://jobs.smartrecruiters.com/{token}/{j['id']}", "posted": iso(j.get("releasedDate")),
                        "detail": ("smartrecruiters", f"https://api.smartrecruiters.com/v1/companies/{token}/postings/{j['id']}")})
        offset += 100
        if offset >= d.get("totalFound", 0) or offset > 1000:
            return out


def workable(token, _):
    d = http(f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=false")
    out = []
    for j in d.get("jobs", []):
        loc = ", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x)
        if j.get("telecommuting"):
            loc += " (remote)"
        out.append({"id": j.get("shortcode"), "title": j.get("title", ""), "locations": [loc],
                    "url": j.get("url") or f"https://apply.workable.com/{token}/j/{j.get('shortcode')}/",
                    "posted": iso(j.get("published_on"))})
    return out


WD_LOC_RX = re.compile(r"canada|ontario|toronto|markham|waterloo|kitchener|mississauga|vaughan|oakville|"
                       r"brampton|guelph|richmond hill|^\s*CAN\b|\bCA-ON\b|,\s*ON\b")


def _wd_facets(facets, picks):
    for f in facets or []:
        param = f.get("facetParameter", "")
        for v in f.get("values", []) or []:
            if "values" in v and isinstance(v.get("values"), list):
                _wd_facets([v], picks)
                continue
            desc = v.get("descriptor", "")
            if v.get("id") and WD_LOC_RX.search(desc) and re.search(r"loc|country|region", param, re.I):
                picks.setdefault(param, []).append((desc, v["id"]))


def workday(tenant, extra):
    wd, site = extra.split("|", 1)
    host = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{host}/wday/cxs/{tenant}/{site}/jobs"
    first = http(api, {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""})
    picks = {}
    _wd_facets(first.get("facets"), picks)
    applied = {}
    # Prefer a country-level "Canada" value; otherwise every matching location value.
    for param, vals in picks.items():
        canada = [i for d, i in vals if d.strip().lower() == "canada"]
        if canada and re.search(r"country", param, re.I):
            applied = {param: canada}
            break
    if not applied:
        for param, vals in picks.items():
            if re.search(r"loc", param, re.I):
                applied.setdefault(param, []).extend(i for _, i in vals)
    search = "" if applied else "Canada"
    out, offset, total = [], 0, None
    while True:
        d = http(api, {"appliedFacets": applied, "limit": 20, "offset": offset, "searchText": search})
        if total is None:
            total = d.get("total", 0) or 0
        posts = d.get("jobPostings", []) or []
        for j in posts:
            path = j.get("externalPath", "")
            loc = j.get("locationsText", "") or ""
            locs = [loc]
            if re.match(r"^\d+ Locations?$", loc.strip()) or "More..." in loc:
                try:
                    det = http(f"{host}/wday/cxs/{tenant}/{site}{path}")
                    info = det.get("jobPostingInfo", {})
                    locs = [info.get("location", "")] + list(info.get("additionalLocations") or [])
                except Exception:
                    locs = [loc + " (check posting)"]
            elif applied and re.match(r"^\s*(remote|home|virtual)[\w\s-]*$", loc, re.I):
                locs = [loc + " (Canada)"]  # Canadian-filtered results that only say "Remote"
            out.append({"id": path.rsplit("_", 1)[-1] or path, "title": j.get("title", ""), "locations": locs,
                        "url": f"{host}/{site}{path}", "posted": j.get("postedOn", ""),
                        "detail": ("workday", f"{host}/wday/cxs/{tenant}/{site}{path}")})
        offset += 20
        if not posts or offset >= total or offset >= 1200:
            return out


def amazon(_, extra):
    out, offset = [], 0
    while True:
        d = http("https://www.amazon.jobs/en/search.json?normalized_country_code%5B%5D=CAN&result_limit=100"
                 f"&sort=recent&offset={offset}")
        jobs = d.get("jobs", []) or []
        for j in jobs:
            out.append({"id": j.get("id_icims") or j.get("id"), "title": j.get("title", ""),
                        "locations": [j.get("normalized_location", ""), j.get("location", "")],
                        "url": "https://www.amazon.jobs" + j.get("job_path", ""), "posted": j.get("posted_date", ""),
                        "desc": " ".join([j.get("description") or "", j.get("basic_qualifications") or "", j.get("preferred_qualifications") or ""])})
        offset += 100
        if not jobs or offset >= (d.get("hits") or 0) or offset >= 1500:
            return out


def microsoft(_, extra):
    out, page = [], 1
    while True:
        d = http(f"https://gcsservices.careers.microsoft.com/search/api/v1/search?lc=Canada&l=en_us&pg={page}&pgSz=20&o=Recent")
        res = ((d or {}).get("operationResult") or {}).get("result") or {}
        jobs = res.get("jobs", []) or []
        for j in jobs:
            props = j.get("properties") or {}
            out.append({"id": j.get("jobId"), "title": j.get("title", ""),
                        "locations": list(props.get("locations") or []) + [props.get("primaryLocation", "")],
                        "url": f"https://jobs.careers.microsoft.com/global/en/job/{j.get('jobId')}", "posted": iso(j.get("postingDate"))})
        page += 1
        if not jobs or (page - 1) * 20 >= (res.get("totalJobs") or 0) or page > 60:
            return out


def uber(_, extra):
    out, page = [], 0
    while True:
        d = http("https://www.uber.com/api/loadSearchJobsResults?localeCode=en",
                 {"params": {"location": [{"country": "CAN", "region": "Ontario", "city": "Toronto"}], "department": []},
                  "page": page, "limit": 50}, headers={"x-csrf-token": "x"})
        res = ((d or {}).get("data") or {}).get("results") or []
        for j in res:
            locs = [", ".join(x for x in [l.get("city"), l.get("region"), l.get("countryName")] if x)
                    for l in (j.get("allLocations") or [j.get("location") or {}])]
            out.append({"id": j.get("id"), "title": j.get("title", ""), "locations": locs,
                        "url": f"https://www.uber.com/global/en/careers/list/{j.get('id')}/", "posted": iso(j.get("creationDate"))})
        page += 1
        if len(res) < 50 or page > 20:
            return out


def eightfold(host, extra):
    domain = extra
    out, start = [], 0
    while True:
        d = http(f"https://{host}/api/apply/v2/jobs?domain={domain}&start={start}&num=50&location=Canada&sort_by=timestamp")
        pos = d.get("positions", []) or []
        for j in pos:
            out.append({"id": j.get("id"), "title": j.get("name", ""),
                        "locations": list(j.get("locations") or []) + [j.get("location", "")],
                        "url": j.get("canonicalPositionUrl") or f"https://{host}/careers/job/{j.get('id')}",
                        "posted": iso(j.get("t_create"))})
        start += 50
        if not pos or start >= (d.get("count") or 0) or start >= 1000:
            return out


def ukg(tenant, board):
    """UKG (UltiPro) job boards, e.g. MDA Space. token = tenant path, extra = board id."""
    base = f"https://recruiting.ultipro.ca/{tenant}/JobBoard/{board}"
    out, skip = [], 0
    while True:
        body = {"opportunitySearch": {"Top": 100, "Skip": skip, "QueryString": "",
                                      "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}],
                                      "Filters": []},
                "matchCriteria": {"PreferredJobs": [], "Educations": [], "LicenseAndCertifications": [], "Skills": [],
                                  "hasNoLicenses": False, "SkippedSkills": []}}
        d = http(f"{base}/JobBoardView/LoadSearchResults", body)
        ops = d.get("opportunities", []) or []
        for o in ops:
            locs = []
            for l in o.get("Locations", []) or []:
                a = l.get("Address") or {}
                st = (a.get("State") or {}).get("Code") or ""
                ctry = (a.get("Country") or {}).get("Name") or ""
                locs.append(", ".join(x for x in [a.get("City") or l.get("LocalizedDescription"), st, ctry] if x))
            out.append({"id": o["Id"], "title": o.get("Title", ""), "locations": locs,
                        "url": f"{base}/OpportunityDetail?opportunityId={o['Id']}", "posted": iso(o.get("PostedDate")),
                        "detail": ("ukg", f"{base}/OpportunityDetail?opportunityId={o['Id']}")})
        skip += 100
        if not ops or skip >= (d.get("totalCount") or 0) or skip > 1000:
            return out


def jibe(host, _):
    """Jibe-powered careers sites (e.g. careers.amd.com): /api/jobs with descriptions included."""
    out, page = [], 1
    while True:
        d = http(f"https://{host}/api/jobs?location=Canada&page={page}&sortBy=posted_date&descending=true&internal=false")
        jobs = d.get("jobs", []) or []
        for x in jobs:
            j = x.get("data") or x
            loc = ", ".join(v for v in [j.get("city"), j.get("state"), j.get("country")] if v)
            out.append({"id": j.get("req_id") or j.get("slug"), "title": j.get("title", ""), "locations": [loc],
                        "url": f"https://{host}/careers-home/jobs/{j.get('req_id') or j.get('slug')}",
                        "posted": iso(j.get("posted_date") or j.get("create_date")), "desc": j.get("description") or ""})
        page += 1
        if len(jobs) < 10 or page > 40:
            return out


def pcsx(host, domain):
    """Eightfold 'pcsx' careers sites (Qualcomm, Microsoft)."""
    out, start, total = [], 0, None
    while True:
        d = http(f"https://{host}/api/pcsx/search?domain={domain}&query=&location=Canada&start={start}&sort_by=timestamp")
        d = (d or {}).get("data") or d or {}
        ps = d.get("positions", []) or []
        total = d.get("count") or 0
        for p in ps:
            out.append({"id": p.get("id"), "title": p.get("name", ""), "locations": list(p.get("locations") or []),
                        "url": f"https://{host}/careers/job/{p.get('id')}", "posted": iso(p.get("postedTs") or p.get("t_create")),
                        "detail": ("pcsx", f"https://{host}/api/pcsx/position_details?position_id={p.get('id')}&domain={domain}&hl=en")})
        start += len(ps)
        if not ps or start >= total or start > 1500:
            return out


def _ukg_desc(html_text):
    m = re.search(r'"Description":"(.*?)","', html_text or "", re.S)
    if not m:
        return ""
    try:
        return json.loads('"' + m.group(1) + '"')
    except Exception:
        return m.group(1)


ADAPTERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters,
            "workable": workable, "workday": workday, "amazon": amazon, "microsoft": microsoft, "uber": uber,
            "eightfold": eightfold, "ukg": ukg, "jibe": jibe, "pcsx": pcsx}


def probe(slugs):
    """Try Greenhouse, Lever and Ashby for each slug; return (ats, token, jobs)."""
    errors = []
    for slug in slugs:
        for ats in ("greenhouse", "lever", "ashby"):
            try:
                jobs = ADAPTERS[ats](slug, "")
                if ats == "ashby" and not jobs:
                    continue
                return ats, slug, jobs
            except Exception as e:
                errors.append(f"{ats}:{slug}:{getattr(e, 'code', type(e).__name__)}")
    raise RuntimeError("no board found (" + ", ".join(errors[:6]) + ")")


# ---------------------------------------------------------------- main
def run_board(row):
    company, ats, token, extra = row["company"], row["ats"], row["token"], row.get("extra", "")
    rec = {"company": company, "ats": ats, "token": token, "ok": False, "seen": 0, "kept": 0, "error": "", "resolved": ""}
    try:
        if ats == "auto":
            ats2, tok2, jobs = probe([s for s in token.split(";") if s])
            rec["resolved"] = f"{ats2}:{tok2}"
            ats_used = ats2
        else:
            jobs = ADAPTERS[ats](token, extra)
            ats_used = ats
        rec["ok"] = True
        rec["seen"] = len(jobs)
        kept = []
        for j in jobs:
            title = (j.get("title") or "").strip()
            keep_t, level, _ = title_verdict(title)
            if not keep_t:
                continue
            keep_l, remote = location_verdict([str(x) for x in j.get("locations", []) if x])
            if not keep_l:
                continue
            desc = j.get("desc") or ""
            if not desc and j.get("detail"):
                kind, durl = j["detail"]
                try:
                    if kind == "ukg":
                        req = urllib.request.Request(durl, headers={"User-Agent": UA})
                        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                            desc = _ukg_desc(r.read().decode("utf-8", "replace"))
                    else:
                        desc = DETAIL[kind](http(durl))
                except Exception:
                    desc = ""
            min_years, years_text, grad_text = extract_quals(desc) if desc else (None, "", "")
            stack, s_has, s_learn, s_miss, req = extract_stack(desc) if desc else ([], [], [], [], "")
            kept.append({
                "id": f"{ats_used}:{token.split(';')[0] if ats == 'auto' else token}:{j['id']}",
                "min_years": min_years, "years_text": years_text, "grad_text": grad_text, "desc_read": bool(desc),
                "stack": stack, "skills_has": s_has, "skills_learning": s_learn, "skills_missing": s_miss,
                "requirements": req,
                "company": company, "title": title,
                "location": "; ".join(dict.fromkeys(str(x) for x in j.get("locations", []) if x))[:300],
                "url": j.get("url", ""), "ats": ats_used, "posted": j.get("posted", ""),
                "level_hint": level, "remote": remote, "pathway_hint": row.get("pathway", ""),
                "tier": row.get("tier", ""),
            })
        rec["kept"] = len(kept)
        return rec, kept
    except Exception as e:
        rec["error"] = (f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else str(e))[:200]
        return rec, []


def main():
    rows = []
    with open(os.path.join(ROOT, "companies.csv"), newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("company", "").startswith("#") or not r.get("ats"):
                continue
            rows.append({k: (v or "").strip() for k, v in r.items()})
    os.makedirs(OUT, exist_ok=True)
    seen_path = os.path.join(OUT, "seen.json")
    try:
        seen = json.load(open(seen_path))
    except Exception:
        seen = {}
    today = dt.datetime.utcnow().strftime("%Y-%m-%d")
    health, feed = [], []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(run_board, r) for r in rows]
        for fut in as_completed(futures):
            rec, kept = fut.result()
            health.append(rec)
            feed.extend(kept)
    # Dedupe by URL (same posting on two boards of one company).
    by_url = {}
    for j in feed:
        by_url.setdefault(j["url"] or j["id"], j)
    feed = list(by_url.values())
    new_seen = {}
    for j in feed:
        j["first_seen"] = seen.get(j["id"], today)
        new_seen[j["id"]] = j["first_seen"]
    feed.sort(key=lambda j: (j["first_seen"], j["company"].lower(), j["title"].lower()), reverse=True)
    health.sort(key=lambda h: (h["ok"], h["company"].lower()))
    now = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    summary = {
        "generated_at": now,
        "boards": len(health), "ok": sum(h["ok"] for h in health), "failed": sum(not h["ok"] for h in health),
        "jobs_seen": sum(h["seen"] for h in health), "jobs_kept": len(feed),
        "new_today": sum(1 for j in feed if j["first_seen"] == today),
    }
    json.dump({"summary": summary, "boards": health}, open(os.path.join(OUT, "health.json"), "w"), indent=1, ensure_ascii=False)
    json.dump({"generated_at": now, "count": len(feed), "jobs": feed}, open(os.path.join(OUT, "feed.json"), "w"), indent=1, ensure_ascii=False)
    json.dump(new_seen, open(seen_path, "w"), indent=0, sort_keys=True)
    print(json.dumps(summary))


if __name__ == "__main__":
    sys.exit(main())
