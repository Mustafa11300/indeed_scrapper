# Indeed Scraper

This repository contains a Playwright-based script for collecting candidate data from Indeed Employer.

## Quick Start

1. Create and activate a virtual environment.
2. Install dependencies and Playwright browsers.
3. Run the scraper.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install
```

## Script

- `harvest.py` walks the candidate list, switches sort order, and saves candidate records to `data/candidates.json` and `data/candidates.csv`.

Run the script with Python:

```bash
python harvest.py
```

## What You Need

- Python 3.10 or newer is recommended.
- Google Chrome or Chromium must be available on the machine.
- The first time you run either script, sign in manually in the browser window that opens.

## Behavior

- The script uses a persistent browser profile stored in `indeed_session/`.
- Existing files in `data/` are reused so runs can resume and deduplicate results.

## Output

- `harvest.py` writes `data/candidates.json` and `data/candidates.csv`

## Resetting

If you want a completely fresh run, delete the saved data and browser session before starting again:

```bash
rm -rf data/* indeed_session/
```

## Troubleshooting

- If the browser opens to a login screen, sign in and rerun the script.
- If Playwright complains about missing browser binaries, run `python -m playwright install` again.