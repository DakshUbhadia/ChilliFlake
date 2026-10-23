import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import os
db_path = os.environ.get("DB_PATH", "db/chilliflake.db")
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

r = conn.execute("""
    SELECT
        (SELECT COUNT(*) FROM builds)           AS builds,
        (SELECT COUNT(*) FROM tests)            AS tests,
        (SELECT COUNT(*) FROM test_runs)        AS test_runs,
        (SELECT COUNT(*) FROM flakiness_scores) AS scores
""").fetchone()
print("=" * 60)
print("DB OVERVIEW")
print("=" * 60)
print(f"  Builds      : {r['builds']}")
print(f"  Tests       : {r['tests']}")
print(f"  Test runs   : {r['test_runs']}")
print(f"  Scores      : {r['scores']}")

print()
print("=" * 60)
print("BUILD TIMELINE")
print("=" * 60)
for row in conn.execute("SELECT github_run_id, branch, created_at FROM builds ORDER BY created_at"):
    print(f"  run={row['github_run_id']}  branch={row['branch']}  at={row['created_at']}")

print()
print("=" * 60)
print("VERDICT BREAKDOWN")
print("=" * 60)
for row in conn.execute("SELECT verdict, COUNT(*) AS c FROM flakiness_scores GROUP BY verdict ORDER BY c DESC"):
    print(f"  {row['verdict']:<22} {row['c']}")

print()
print("=" * 60)
print("SAMPLE SIZE DISTRIBUTION (non-skipped runs per test)")
print("=" * 60)
for row in conn.execute("""
    SELECT sample_size, COUNT(*) AS c
    FROM flakiness_scores
    GROUP BY sample_size
    ORDER BY sample_size
"""):
    print(f"  n={row['sample_size']:<4}  tests={row['c']}")

print()
print("=" * 60)
print("WLB DISTRIBUTION (flaky threshold = 0.15)")
print("=" * 60)
buckets = [
    ("0.00 – 0.02 (very stable)",    "wilson_lower_bound >= 0.00 AND wilson_lower_bound < 0.02"),
    ("0.02 – 0.05 (stable)",         "wilson_lower_bound >= 0.02 AND wilson_lower_bound < 0.05"),
    ("0.05 – 0.10 (low signal)",     "wilson_lower_bound >= 0.05 AND wilson_lower_bound < 0.10"),
    ("0.10 – 0.15 (borderline)",     "wilson_lower_bound >= 0.10 AND wilson_lower_bound < 0.15"),
    ("0.15+       (FLAKY)",          "wilson_lower_bound >= 0.15"),
]
for label, cond in buckets:
    n = conn.execute(f"SELECT COUNT(*) AS c FROM flakiness_scores WHERE {cond}").fetchone()["c"]
    print(f"  {label:<32}  {n}")

print()
print("=" * 60)
print("TOP 20 TESTS BY FLIP RATE (candidates to watch)")
print("=" * 60)
for row in conn.execute("""
    SELECT
        t.file_path, t.test_name,
        fs.flip_rate, fs.wilson_lower_bound, fs.sample_size,
        fs.pass_rate, fs.verdict
    FROM flakiness_scores fs
    JOIN tests t ON t.id = fs.test_id
    WHERE fs.sample_size >= 3
    ORDER BY fs.flip_rate DESC, fs.wilson_lower_bound DESC
    LIMIT 20
"""):
    verdict_tag = f"[{row['verdict'].upper():<17}]"
    print(f"  {verdict_tag} fr={row['flip_rate']:.4f} wlb={row['wilson_lower_bound']:.4f} "
          f"n={row['sample_size']} pass={row['pass_rate']:.2f}")
    print(f"    {row['file_path'] or '(no path)'} :: {row['test_name']}")

print()
print("=" * 60)
print("FLAKY TESTS (verdict=flaky)")
print("=" * 60)
flaky = conn.execute("""
    SELECT t.file_path, t.test_name,
           fs.flip_rate, fs.wilson_lower_bound, fs.sample_size, fs.pass_rate
    FROM flakiness_scores fs
    JOIN tests t ON t.id = fs.test_id
    WHERE fs.verdict = 'flaky'
    ORDER BY fs.wilson_lower_bound DESC
    LIMIT 20
""").fetchall()
if flaky:
    for row in flaky:
        print(f"  wlb={row['wilson_lower_bound']:.4f} fr={row['flip_rate']:.4f} "
              f"n={row['sample_size']} pass={row['pass_rate']:.2f}")
        print(f"    {row['file_path'] or '(no path)'} :: {row['test_name']}")
else:
    print("  None found yet — see explanation below.")

print()
print("=" * 60)
print("BROKEN TESTS (verdict=broken, pass_rate < 5%)")
print("=" * 60)
broken = conn.execute("""
    SELECT t.file_path, t.test_name, fs.pass_rate, fs.sample_size
    FROM flakiness_scores fs
    JOIN tests t ON t.id = fs.test_id
    WHERE fs.verdict = 'broken'
    ORDER BY fs.pass_rate ASC
    LIMIT 10
""").fetchall()
if broken:
    for row in broken:
        print(f"  pass_rate={row['pass_rate']:.2f} n={row['sample_size']}  "
              f"{row['file_path']} :: {row['test_name']}")
else:
    print("  None found.")

print()
print("=" * 60)
print("DIAGNOSIS")
print("=" * 60)
max_wlb = conn.execute("SELECT MAX(wilson_lower_bound) AS m FROM flakiness_scores").fetchone()["m"]
max_wlb = max_wlb or 0.0
conflict_tests = conn.execute("SELECT COUNT(*) FROM flakiness_scores WHERE commit_conflicts > 0").fetchone()[0]
analysis_branch = conn.execute("SELECT DISTINCT analysis_branch FROM flakiness_scores LIMIT 1").fetchone()
analysis_branch = '(all)' if analysis_branch and analysis_branch[0] == '*' else (analysis_branch[0] if analysis_branch else '(none)')
n_builds = conn.execute("SELECT COUNT(*) AS c FROM builds").fetchone()["c"]
unique_days = conn.execute("""
    SELECT COUNT(DISTINCT DATE(created_at)) AS d FROM builds
""").fetchone()["d"]
print(f"  Builds ingested : {n_builds}")
print(f"  Branch scope    : {analysis_branch}")
print(f"  Commit conflicts: {conflict_tests} test(s)")
print(f"  Unique days     : {unique_days}")
print(f"  Highest WLB     : {max_wlb:.4f}  (threshold = 0.15)")
print()
if max_wlb < 0.15:
    print("  Check the analyzer branch scope and inspect same-commit conflicts.")

conn.close()
