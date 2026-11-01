# job-feed

A daily feed of new-grad and junior software postings in Toronto/GTA, Hamilton, Niagara, Waterloo region and remote Canada, read straight from employers' own job boards.

- `companies.csv`: the boards to read. `ats` is greenhouse, lever, ashby, workday, smartrecruiters, workable, amazon, microsoft, uber, eightfold, or `auto` (tries Greenhouse, Lever and Ashby with each `;`-separated slug).
- `scraper/feed.py`: reads every board, keeps software roles in the target area that aren't internships or senior roles, and writes `feed/`.
- `feed/feed.json`: kept postings with `first_seen`, `level_hint` (junior, mid, unlevelled) and `remote`.
- `feed/health.json`: per-board result (ok, seen, kept, error). An `auto` board that worked shows what it resolved to; pin that in `companies.csv`.
- Runs daily at 9:17 UTC via GitHub Actions, and on any change to the company list or scraper.

Only public job postings live here. Filters, scoring and decisions happen in the morning run, not in this repo.
