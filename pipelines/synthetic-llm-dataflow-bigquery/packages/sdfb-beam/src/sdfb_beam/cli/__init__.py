"""CLI entrypoints for sdfb-beam.

`run_pipeline.py` is the Flex Template `FLEX_TEMPLATE_PYTHON_PY_FILE`
target invoked by the python_template_launcher on Dataflow. The same
script also runs under DirectRunner with `--client_type=fake` or
`--client_type=mlx` for M4 smoke testing (see docs/M4_LOCAL_SMOKE.md).
"""
