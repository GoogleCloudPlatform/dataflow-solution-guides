#  Copyright 2026 The synthetic-llm-dataflow-bigquery Authors
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
mock_provider "random" {}

variables {
  project_id = "synthetic-test-project"
  region     = "us-central1"
}

run "deployment_contract" {
  command = plan

  assert {
    condition     = module.source_dataset.dataset_id != null && var.bq_location == "US"
    error_message = "Datasets must be colocated with bigquery-public-data.thelook_ecommerce (US)."
  }
  assert {
    condition     = strcontains(local.snapshot_sql, "EXCEPT (user_geom)") && !strcontains(local.snapshot_sql, "distribution_center_geom")
    error_message = "GEOGRAPHY columns must stay out of the source snapshots."
  }
  assert {
    condition     = strcontains(local.snapshot_sql, "`synthetic-test-project.synthetic_data.products`")
    error_message = "The products catalog must land in the landing dataset: external FK parents resolve there."
  }
  assert {
    condition     = google_bigquery_table.quality["dlq"].time_partitioning[0].field == "dlq_inserted_at" && length(google_bigquery_table.quality["fk_fanout_stats"].time_partitioning) == 0
    error_message = "Quality tables must follow docs/DEPLOYMENT_PREREQUISITES.md partitioning."
  }
  assert {
    condition     = local.model_paths[var.model] == "gemma4/e4b-it/v1"
    error_message = "Gemma 4 E4B-it on L4 is the default model."
  }
  assert {
    condition     = length(module.buckets) == 0 && length(google_compute_subnetwork_iam_member.dataflow_network_user) == 0
    error_message = "Existing bucket and default network are the defaults."
  }
}

run "subnetwork_and_bucket_options" {
  command = plan

  variables {
    subnetwork    = "https://www.googleapis.com/compute/v1/projects/host-project/regions/us-central1/subnetworks/shared"
    create_bucket = true
    bucket_name   = "synthetic-test-bucket"
    model         = "qwen3-4b"
  }

  assert {
    condition     = google_compute_subnetwork_iam_member.dataflow_network_user[0].project == "host-project" && google_compute_subnetwork_iam_member.dataflow_network_user[0].subnetwork == "shared"
    error_message = "Shared VPC subnet IAM must target the host project's subnet."
  }
  assert {
    condition     = length(module.buckets) == 1 && local.models_prefix == "gs://synthetic-test-bucket/synthetic/models"
    error_message = "A created bucket must also host the model weights."
  }
  assert {
    condition     = local.model_paths[var.model] == "qwen3/4b-instruct-2507/v1"
    error_message = "qwen3-4b must map to its staged GCS layout."
  }
}
