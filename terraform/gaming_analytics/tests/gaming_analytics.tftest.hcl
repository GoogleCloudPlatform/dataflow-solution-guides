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
  project_id = "gaming-test-project"
  region     = "us-central1"
}

run "cpu_defaults" {
  command = plan

  assert {
    condition     = local.machine_type == "n1-standard-2"
    error_message = "The model runs on the worker CPU, so the default worker must be a CPU machine type."
  }
  assert {
    condition     = google_bigtable_table_iam_member.worker_features.table == "player_features" && google_bigtable_table_iam_member.worker_features.role == "roles/bigtable.reader"
    error_message = "Worker enrichment permissions must be table scoped and read only."
  }
  assert {
    condition     = google_bigquery_table.player_recommendations.time_partitioning[0].field == "event_timestamp"
    error_message = "Analytics partitioning must use event time, not processing time."
  }
  assert {
    condition     = length(module.buckets) == 0
    error_message = "Existing bucket reuse is the default."
  }
  assert {
    condition     = length(google_compute_subnetwork_iam_member.dataflow_network_user) == 0
    error_message = "No subnetwork IAM binding must be created when no subnetwork is configured."
  }
  assert {
    condition     = !contains(local.services, "aiplatform.googleapis.com")
    error_message = "The pipeline scores locally with scikit-learn: Vertex AI must not be enabled."
  }
  assert {
    condition     = local.worker_disk_size_gb == 50
    error_message = "CPU workers do not need the large disk a GPU image required."
  }
}

run "explicit_machine_type" {
  command = plan

  variables {
    machine_type = "n2-standard-4"
  }

  assert {
    condition     = local.machine_type == "n2-standard-4"
    error_message = "An explicit machine type must override the default."
  }
}

run "shared_vpc_subnetwork" {
  command = plan

  variables {
    subnetwork = "https://www.googleapis.com/compute/v1/projects/host-project/regions/europe-southwest1/subnetworks/dataflow-subnet"
  }

  assert {
    condition     = google_compute_subnetwork_iam_member.dataflow_network_user[0].project == "host-project"
    error_message = "The subnetwork IAM binding must target the Shared VPC host project."
  }
  assert {
    condition     = google_compute_subnetwork_iam_member.dataflow_network_user[0].subnetwork == "dataflow-subnet"
    error_message = "The subnetwork name must be resolved from the full URI."
  }
}
