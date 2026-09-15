"""Beam-layer codegen.

Lives here (not in `sdfb_core.codegen`) because `pandera` + `pandas` belong
to the Beam-layer dep surface — `sdfb_core` stays installable on the
laptop with `pydantic` alone.
"""

from sdfb_beam.codegen.derive_pandera import derive_pandera_schema

__all__ = ["derive_pandera_schema"]
