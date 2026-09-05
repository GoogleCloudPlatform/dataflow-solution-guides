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
mock_provider "google" {}
mock_provider "google-beta" {}
mock_provider "local" {}

variables {
  project_id = "anomaly-test-project"
  region     = "us-central1"
}

run "application_contract" {
  command = plan

  assert {
    condition     = local.machine_type == "n1-standard-2"
    error_message = "The inference client must use a CPU worker."
  }
  assert {
    condition     = google_bigtable_table_iam_member.worker_features.table == "customer_profiles" && google_bigtable_table_iam_member.worker_features.role == "roles/bigtable.reader"
    error_message = "Worker enrichment permissions must be table scoped and read only."
  }
  assert {
    condition     = google_project_iam_custom_role.predictor.permissions == toset(["aiplatform.endpoints.predict"])
    error_message = "Workers may only predict, never train or deploy."
  }
  assert {
    condition     = google_bigquery_table.detections.time_partitioning[0].field == "timestamp"
    error_message = "Archive partitioning must use transaction time."
  }
  assert {
    condition     = length(module.buckets) == 0
    error_message = "Existing bucket reuse is the default."
  }
  assert {
    condition     = google_storage_bucket_iam_member.training_objects.condition[0].expression == "resource.name.startsWith('projects/_/buckets/anomaly-test-project/objects/anomaly-training/')"
    error_message = "Training writes must stay within the training artifact prefix."
  }
}
