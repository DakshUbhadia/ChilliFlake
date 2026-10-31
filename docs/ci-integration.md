# Consume ChilliFlake quarantine data in pytest

Copy `src/analyzer/pytest_plugin.py` into the target repository (for example as
`chilliflake_plugin.py`) and commit the generated `quarantine.yaml` alongside
the workflow. The plugin has no runtime dependency for YAML parsing beyond
PyYAML:

```yaml
- run: pip install pytest pyyaml
- run: pytest -p chilliflake_plugin
  env:
    CHILLIFLAKE_QUARANTINE_FILE: quarantine.yaml
```

The YAML document must use `schema_version: 1`. The plugin checks its shape and
logs a warning while quarantining nothing if the file is missing or malformed.
Quarantined tests run with `xfail(strict=False)` by default so failures remain
visible without failing the job. Set `CHILLIFLAKE_QUARANTINE_MODE=skip` to skip
them instead. Legacy JSON exports are still accepted when the file ends in
`.json`.
