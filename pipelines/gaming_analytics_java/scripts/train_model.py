#  Copyright 2026 Google LLC
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""Trains the in-game recommendation model used by the gaming analytics pipeline.

The Java pipeline scores gameplay events with the Apache Beam
``RunInference`` transform through its cross-language wrapper, which delegates
to the Python ``SklearnModelHandlerNumpy`` model handler. That handler
unpickles the artifact produced here inside the Python SDK harness, so the
scikit-learn and NumPy versions used to pickle the model must match the
versions installed in the harness.

Normally this script runs *inside* the harness container build (see
``../Dockerfile``), which makes that match automatic: the interpreter that
pickles the artifact is the very one that later unpickles it. The pins are
still declared in ``requirements-model.txt`` and mirrored in
``RecommendationInference.java``, and this script refuses to run when the
local environment does not match them, so that running it by hand on a
workstation cannot silently produce an unloadable artifact.

The model is deliberately tiny and explainable: a depth-limited decision tree.
It is a reference guide, not a real recommender.

``SklearnModelHandlerNumpy`` calls ``model.predict()`` and never
``predict_proba()``, so the model is fitted as a *multi-output regressor* on
one-hot encoded targets. ``predict()`` then returns one propensity per
recommendation, and the pipeline picks the highest scoring one. That is what
lets the pipeline report both a label and a confidence with a single call.
"""

import argparse
import os
import pickle
import sys
from typing import List, Tuple
import numpy as np
import sklearn
from sklearn.tree import DecisionTreeRegressor

# Versions the model artifact is pickled with. They are the versions the Apache
# Beam 2.76.0 Python SDK harness image ships
# (sdks/python/container/py3XX/base_image_requirements.txt), which is what the
# Dataflow workers unpickle the artifact with. The same pins appear in
# scripts/requirements.txt and in
# RecommendationInference.HARNESS_REQUIREMENTS in
# src/main/java/.../gaming_analytics/transform/RecommendationInference.java.
# Unpickling a scikit-learn estimator with a different minor version fails, or
# worse, silently misbehaves, so all three copies move together with the Beam
# version.
PINNED_SKLEARN_VERSION = "1.7.2"
PINNED_NUMPY_VERSION = "2.4.6"

# Feature vector layout. The order is part of the contract with
# RecommendationInference.toFeatureVector() on the Java side: changing it here
# without changing it there silently produces nonsense predictions.
FEATURE_NAMES = [
    "level",
    "score",
    "churn_risk",
    "level_failures",
    "skill_rating",
    "spend_tier_score",
    "event_type_code",
]

# Class index -> recommendation label. Must match RECOMMENDATION_LABELS in
# RecommendationInference.java.
RECOMMENDATION_LABELS = [
    "retention_bonus_pack",
    "difficulty_assist_boost",
    "premium_bundle_offer",
    "tournament_invite",
    "daily_quest_suggestion",
]

# Ordinal encodings, mirrored on the Java side.
SPEND_TIERS = {"free": 0.0, "paying": 1.0, "whale": 2.0}
EVENT_TYPES = {
    "level_start": 0.0,
    "level_complete": 1.0,
    "level_failed": 2.0,
    "purchase": 3.0,
    "item_used": 4.0,
}

CHURN_RISK_THRESHOLD = 0.7
LEVEL_FAILURE_THRESHOLD = 3.0


def check_pinned_versions() -> None:
  """Fails fast when the local versions differ from the pinned ones."""
  mismatches = []
  if sklearn.__version__ != PINNED_SKLEARN_VERSION:
    mismatches.append(
        f"scikit-learn {sklearn.__version__} != {PINNED_SKLEARN_VERSION}")
  if np.__version__ != PINNED_NUMPY_VERSION:
    mismatches.append(f"numpy {np.__version__} != {PINNED_NUMPY_VERSION}")
  if mismatches:
    print("ERROR: the model must be pickled with the pinned versions, "
          "otherwise the Python SDK harness cannot unpickle it.")
    for mismatch in mismatches:
      print(f"  {mismatch}")
    print("Install them with: pip install -r scripts/requirements-model.txt")
    sys.exit(1)


def label_for(features: np.ndarray) -> int:
  """Returns the recommendation index for one feature vector.

  These are the rules the tree is fitted on. They are the same rules the guide
  documents, so that the model stays explainable while still being a real
  scikit-learn artifact loaded by RunInference.

  Args:
      features: one row laid out as FEATURE_NAMES.

  Returns:
      The index into RECOMMENDATION_LABELS.
  """
  level, score, churn_risk, level_failures, skill_rating, spend_tier, event = (
      features)
  if churn_risk >= CHURN_RISK_THRESHOLD:
    return 0
  if event == EVENT_TYPES["level_failed"]:
    return 1
  paying = spend_tier >= SPEND_TIERS["paying"]
  if paying and event in (EVENT_TYPES["level_complete"],
                          EVENT_TYPES["purchase"]):
    return 2
  if skill_rating > 0 and score > skill_rating * 1.5:
    return 3
  del level
  del level_failures
  return 4


def generate_synthetic_data(
    num_samples: int = 20000,
    random_seed: int = 42) -> Tuple[np.ndarray, np.ndarray]:
  """Generates a synthetic gameplay dataset and its one-hot targets.

  Args:
      num_samples: number of rows to generate.
      random_seed: seed, so that the artifact is reproducible.

  Returns:
      A tuple of the feature matrix and the one-hot target matrix.
  """
  rng = np.random.default_rng(random_seed)

  level = rng.integers(1, 60, size=num_samples).astype(float)
  score = rng.gamma(shape=2.0, scale=2500.0, size=num_samples)
  churn_risk = rng.beta(a=2.0, b=5.0, size=num_samples)
  level_failures = rng.poisson(lam=1.5, size=num_samples).astype(float)
  skill_rating = rng.normal(loc=1200.0, scale=450.0, size=num_samples)
  skill_rating = np.clip(skill_rating, 100.0, 3000.0)
  spend_tier_score = rng.choice([0.0, 1.0, 2.0],
                                p=[0.70, 0.25, 0.05],
                                size=num_samples)
  event_type_code = rng.choice(
      list(EVENT_TYPES.values()),
      p=[0.20, 0.20, 0.30, 0.15, 0.15],
      size=num_samples)

  x = np.column_stack([
      level,
      score,
      churn_risk,
      level_failures,
      skill_rating,
      spend_tier_score,
      event_type_code,
  ])

  y = np.zeros((num_samples, len(RECOMMENDATION_LABELS)))
  for i in range(num_samples):
    y[i, label_for(x[i])] = 1.0
  return x, y


def train(num_samples: int = 20000,
          random_seed: int = 42) -> DecisionTreeRegressor:
  """Fits the multi-output decision tree and reports its accuracy."""
  x, y = generate_synthetic_data(num_samples, random_seed)
  model = DecisionTreeRegressor(
      max_depth=8, min_samples_leaf=20, random_state=random_seed)
  model.fit(x, y)

  predicted = np.argmax(model.predict(x), axis=1)
  expected = np.argmax(y, axis=1)
  accuracy = float(np.mean(predicted == expected))
  print(f"Trained on {num_samples} synthetic events.")
  print(f"Top-1 agreement with the reference rules: {accuracy:.2%}")
  for index, name in enumerate(FEATURE_NAMES):
    print(f"  importance[{name}] = {model.feature_importances_[index]:.4f}")
  return model


def upload_to_gcs(local_path: str, model_uri: str) -> None:
  """Uploads the pickled model to Cloud Storage.

  Args:
      local_path: the local pickle produced by this script.
      model_uri: destination, as gs://BUCKET/OBJECT.
  """
  from google.cloud import storage  # pylint: disable=import-outside-toplevel

  without_scheme = model_uri[len("gs://"):]
  bucket_name, _, object_name = without_scheme.partition("/")
  if not bucket_name or not object_name:
    print(f"Error: '{model_uri}' is not a gs://BUCKET/OBJECT URI.")
    sys.exit(1)

  client = storage.Client()
  bucket = client.bucket(bucket_name)
  bucket.blob(object_name).upload_from_filename(local_path)
  print(f"Uploaded the model artifact to: {model_uri}")


def parse_args(argv: List[str]) -> argparse.Namespace:
  """Parses command line arguments."""
  parser = argparse.ArgumentParser(
      description="Train and publish the gaming recommendation model.")
  parser.add_argument(
      "--output_path",
      default="gaming_recommender.pkl",
      help="Local path of the pickled model artifact.",
  )
  parser.add_argument(
      "--model_uri",
      default=os.environ.get("MODEL_URI", ""),
      help="gs://BUCKET/OBJECT the artifact is uploaded to (defaults to "
      "$MODEL_URI). Leave empty to only write the local file.",
  )
  parser.add_argument(
      "--num_samples",
      type=int,
      default=20000,
      help="Number of synthetic training rows.",
  )
  parser.add_argument(
      "--random_seed",
      type=int,
      default=42,
      help="Seed, so that the artifact is reproducible.",
  )
  return parser.parse_args(argv)


def main(argv: List[str]) -> None:
  """CLI entry point."""
  args = parse_args(argv)
  check_pinned_versions()

  model = train(args.num_samples, args.random_seed)
  with open(args.output_path, "wb") as artifact:
    pickle.dump(model, artifact)
  print(f"Saved the model artifact to: {args.output_path}")

  if args.model_uri:
    if not args.model_uri.startswith("gs://"):
      print(f"Error: --model_uri must start with gs://, got '{args.model_uri}'")
      sys.exit(1)
    upload_to_gcs(args.output_path, args.model_uri)
  else:
    print("No --model_uri was given, so the artifact was not uploaded to "
          "Cloud Storage. That is the normal path: this script runs inside "
          "the container build (see ../Dockerfile), which keeps the artifact "
          "at the local path above and lets the Python SDK harness read it "
          "from there without a per-worker download.")


if __name__ == "__main__":
  main(sys.argv[1:])
