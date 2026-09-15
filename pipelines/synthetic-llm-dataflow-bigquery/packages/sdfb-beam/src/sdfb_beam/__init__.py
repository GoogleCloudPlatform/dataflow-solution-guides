"""Apache Beam pipeline + DoFns for synthetic-dataflow-bigquery.

Adds Beam, GCP, and validation library imports on top of `sdfb_core`.
Optional extras `[gpu]`, `[embedding]`, and `[library]` gate the heavy
dependencies that only run on Dataflow workers, never on the laptop.
"""

__version__ = "0.1.0"
