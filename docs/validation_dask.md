# Dask CI history validation

ChilliFlake was validated against JUnit XML produced by the real
[`dask/dask`](https://github.com/dask/dask) GitHub Actions `Tests` workflow.
The workflow runs pytest with `--junit-xml=pytest.xml`, uploads one report per
matrix job, and runs scheduled builds twice daily to expose flaky behavior.

## Preserved validation dataset

The local validation database at `db/chilliflake.db` contains:

- 20 GitHub Actions workflow runs from 2026-08-15 through 2026-08-18;
- 8 runs from the `main` branch and 12 runs from pull-request branches;
- 19,920 distinct Dask test identities;
- 6,422,260 JUnit testcase observations;
- 18,481 scores in the `main` analysis scope;
- 18,480 stable verdicts and one flaky verdict.

The flagged test was
`dask/bytes/tests/test_http.py::test_parquet@http`. It had 64 observations,
a flip rate of approximately 0.0317, a Wilson lower bound of approximately
0.00875, and a same-commit pass/fail conflict. The conflict is why it was
classified as flaky even though its aggregate Wilson bound was below the
general flip threshold.

These counts can be inspected without network access:

```sh
python scripts/query_results.py
```

## Reproduce with current workflow artifacts

The current project configuration selects the `Tests` workflow, `main` branch,
and artifact names beginning with `ubuntu`. A valid fine-grained token with
`Actions: read` avoids anonymous rate limits and is required by GitHub when the
artifact archive endpoint does not permit anonymous download.

```sh
python -m src.ingestion.run_ingestion \
  --project dask --runs 20 --created-after all --db db/dask-validation.db
python -m src.analyzer.run_analysis \
  --project dask --branch main --db db/dask-validation.db
python -m src.analyzer.quarantine \
  --db db/dask-validation.db sync --project dask --branch main
python -m src.analyzer.quarantine \
  --db db/dask-validation.db export --project dask --branch main \
  --out dask-quarantine.yaml
```

GitHub artifact retention means an exact historical rerun may no longer be
downloadable. The SQLite database is the durable record of the original
reports and scores.
