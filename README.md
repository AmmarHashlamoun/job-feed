# job-feed

A daily feed of new-grad and junior software postings in Toronto/GTA, Hamilton, Niagara, Waterloo region and remote Canada, read straight from employers' own job boards.

- `companies.csv`: the boards to read. `ats` is greenhouse, lever, ashby, workday, smartrecruiters, workable, amazon, microsoft, uber, eightfold, or `auto` (tries Greenhouse, Lever and Ashby with each `;`-separated slug).
- `scraper/feed.py`: reads every board, keeps software roles in the target area that aren't co-op, student-term or senior roles (internships open to graduates are kept; `student_rule` flags enrolment wording), and writes `feed/`.
- `feed/feed.json`: kept postings with `first_seen`, `level_hint` (junior, mid, unlevelled), `remote`, and from the description `min_years`, `years_text`, `grad_text`, `stack` (tech named), `skills_has`, `skills_learning`, `skills_missing` and `requirements` (up to 900 characters from the requirements section).
- `feed/health.json`: per-board result (ok, seen, kept, error). An `auto` board that worked shows what it resolved to; pin that in `companies.csv`.
- Runs three times overnight (3:23, 6:41 and 8:53 UTC) via GitHub Actions, since GitHub can start scheduled runs hours late, and on any change to the company list or scraper.

Only public job postings live here. Filters, scoring and decisions happen in the morning run, not in this repo.
