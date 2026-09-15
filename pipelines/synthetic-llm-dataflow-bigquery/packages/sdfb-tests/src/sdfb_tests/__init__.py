"""Test suite for synthetic-dataflow-bigquery.

Unit tests run on the laptop by default. Tests marked `@pytest.mark.gpu` or
`@pytest.mark.gcp` are skipped unless explicitly selected — they require the
M4 Pro with GPU access or live GCP credentials.

This `src/sdfb_tests/` package holds reusable test helpers (fake clients,
fixtures, hypothesis strategies). Actual pytest discovery happens in the
sibling `tests/` directory.
"""
