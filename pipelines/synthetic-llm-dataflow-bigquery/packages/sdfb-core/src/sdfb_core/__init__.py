"""Pure-Python core for synthetic-dataflow-bigquery.

This package contains the engine ABC, Pydantic contracts, codegen utilities,
and prompt templates. It must remain importable on the laptop with **no**
Beam, GCP, or torch dependencies — engines are testable in isolation under
DirectRunner + FakeModelClient.
"""

__version__ = "0.1.0"
