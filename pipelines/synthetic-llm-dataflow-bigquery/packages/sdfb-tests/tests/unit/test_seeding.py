# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=missing-module-docstring
from sdfb_core.seeding import derive_batch_seed, derive_key_seed


def test_deterministic_per_run_and_batch():
  assert derive_batch_seed("run-a", 0) == derive_batch_seed("run-a", 0)


def test_default_salt_keeps_every_existing_seed_byte_identical():
  assert derive_key_seed("r", ("k",)) == derive_key_seed("r", ("k",), salt="")


def test_nonempty_salt_changes_the_seed():
  assert derive_key_seed("r", ("k",)) != derive_key_seed(
      "r", ("k",), salt="T,R")


def test_batches_differ():
  seeds = {derive_batch_seed("run-a", i) for i in range(100)}
  assert len(seeds) == 100


def test_runs_differ():
  assert derive_batch_seed("run-a", 0) != derive_batch_seed("run-b", 0)


def test_fits_in_signed_64bit_and_nonnegative():
  s = derive_batch_seed("x" * 500, 10**9)
  assert 0 <= s < 2**63
