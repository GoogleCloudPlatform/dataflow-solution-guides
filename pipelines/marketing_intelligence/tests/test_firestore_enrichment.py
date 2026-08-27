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
"""Unit tests for FirestoreEnrichmentDoFn."""

import unittest
from unittest.mock import MagicMock
from marketing_intelligence_pipeline.pipeline import FirestoreEnrichmentDoFn


class TestFirestoreEnrichmentDoFn(unittest.TestCase):
  """Unit tests for FirestoreEnrichmentDoFn customer profile lookups."""

  def test_enrichment_with_existing_profile(self):
    """Verifies that an existing customer profile is merged into event."""
    dofn = FirestoreEnrichmentDoFn(
        project="test-project", collection="customer_profiles")
    dofn.setup()

    # Mock Firestore Client & Document
    mock_client = MagicMock()
    mock_doc = MagicMock()
    mock_doc.exists = True
    mock_doc.to_dict.return_value = {
        "loyalty_tier": "Gold",
        "total_lifetime_spend": 1450.50,
        "total_orders": 18,
        "days_since_last_order": 12,
        "preferred_category": "electronics",
    }
    mock_client.collection().document().get.return_value = mock_doc
    dofn.client = mock_client

    raw_event = {
        "event_id": "evt_101",
        "user_id": "user_1042",
        "item_id": "item_201",
        "item_category": "electronics",
        "session_duration_sec": 120,
        "cart_value": 45.0,
    }

    results = list(dofn.process(raw_event))
    self.assertEqual(len(results), 1)
    enriched = results[0]

    self.assertEqual(enriched["user_id"], "user_1042")
    self.assertEqual(enriched["loyalty_tier"], "Gold")
    self.assertEqual(enriched["total_lifetime_spend"], 1450.50)
    self.assertEqual(enriched["total_orders"], 18)
    self.assertEqual(enriched["days_since_last_order"], 12)
    self.assertEqual(enriched["preferred_category"], "electronics")

  def test_enrichment_cold_start_user(self):
    """Verifies that missing/unregistered users receive safe defaults."""
    dofn = FirestoreEnrichmentDoFn(
        project="test-project", collection="customer_profiles")
    dofn.setup()

    mock_client = MagicMock()
    mock_doc = MagicMock()
    mock_doc.exists = False
    mock_client.collection().document().get.return_value = mock_doc
    dofn.client = mock_client

    raw_event = {
        "event_id": "evt_999",
        "user_id": "user_unknown_999",
        "item_id": "item_100",
        "item_category": "home",
    }

    results = list(dofn.process(raw_event))
    self.assertEqual(len(results), 1)
    enriched = results[0]

    self.assertEqual(enriched["loyalty_tier"], "Bronze")
    self.assertEqual(enriched["total_lifetime_spend"], 0.0)
    self.assertEqual(enriched["total_orders"], 0)
    self.assertEqual(enriched["days_since_last_order"], 999)
    self.assertEqual(enriched["preferred_category"], "unknown")

  def test_cache_hit_avoids_repeated_queries(self):
    """Verifies that LRU cache serves repeated requests without querying Firestore."""
    dofn = FirestoreEnrichmentDoFn(
        project="test-project", collection="customer_profiles")
    dofn.setup()

    mock_client = MagicMock()
    mock_doc = MagicMock()
    mock_doc.exists = True
    mock_doc.to_dict.return_value = {
        "loyalty_tier": "Platinum",
        "total_lifetime_spend": 5000.0,
        "total_orders": 50,
        "days_since_last_order": 2,
        "preferred_category": "sports",
    }
    mock_client.collection().document().get.return_value = mock_doc
    dofn.client = mock_client

    event = {"user_id": "user_2000", "event_id": "e1"}

    # First call - cache miss
    _ = list(dofn.process(event))
    self.assertEqual(mock_client.collection().document().get.call_count, 1)

    # Second call - cache hit
    _ = list(dofn.process(event))
    self.assertEqual(mock_client.collection().document().get.call_count, 1)


if __name__ == "__main__":
  unittest.main()
