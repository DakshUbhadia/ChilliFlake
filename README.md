# 🌶️ ChilliFlake

**A statistical flaky test detector and pipeline quarantine system for CI data from `microsoft/playwright`.**

ChilliFlake ingests GitHub Actions CI run data, statistically identifies flaky tests, and automatically quarantines them so they don't block your pipeline.

---

## Features

- 📥 **Ingestion** — Pulls CI workflow run data from the GitHub API
- 📊 **Analyzer** — Detects flaky tests using statistical pass/fail rate analysis
- 🚧 **Quarantine** — Flags and isolates identified flaky tests from blocking pipelines
- 📈 **Dashboard** — Streamlit-powered UI for visualizing flake trends
- 🗄️ **SQLite Storage** — Lightweight, zero-config local database

---

## Project Structure

```
chilliflake/
├── db/                   # SQLite database files (git-ignored)
├── src/
│   ├── ingestion/        # GitHub API data fetching
│   ├── analyzer/         # Flake detection logic
│   └── quarantine/       # Quarantine management
├── dashboard/            # Streamlit dashboard app
├── tests/                # Pytest test suite
├── requirements.txt
└── README.md
```

---

## Quickstart

```bash
# 1. Create and activate a virtual environment
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set your GitHub token
cp .env.example .env
# Edit .env and fill in GITHUB_TOKEN

# 4. Run ingestion
python -m src.ingestion.fetcher

# 5. Run the analyzer
python -m src.analyzer.detector

# 6. Launch the dashboard
streamlit run dashboard/app.py
```

---

## Environment Variables

| Variable        | Description                         |
|-----------------|-------------------------------------|
| `GITHUB_TOKEN`  | GitHub personal access token (PAT)  |
| `DB_PATH`       | Path to SQLite database file        |

---

## Running Tests

```bash
pytest tests/
```

---

## License

MIT
