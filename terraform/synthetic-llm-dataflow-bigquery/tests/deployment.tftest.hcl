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
    condition = toset(keys(local.pipeline_tables)) == toset([
      "synthetic_data_quality/dlq", "synthetic_data_quality/fk_fanout_stats",
      "synthetic_data_quality/validation_runs", "synthetic_rag/freetext_pools",
      "synthetic_rag/rag_chunks", "synthetic_rag/source_table_stats",
    ]) && local.pipeline_datasets == toset([local.quality_dataset, local.rag_dataset])
    error_message = "Pipeline tables must be exactly the files in config/bq_schema/<dataset>/<table>.schema.json."
  }
  assert {
    condition     = google_bigquery_table.pipeline["synthetic_rag/freetext_pools"].table_id == "freetext_pools" && google_bigquery_table.pipeline["synthetic_rag/freetext_pools"].schema == file("${local.schema_dir}/synthetic_rag/freetext_pools.schema.json")
    error_message = "A pipeline table takes its name and schema from its schema file."
  }
  assert {
    condition     = google_bigquery_table.pipeline["synthetic_data_quality/dlq"].time_partitioning[0].field == "dlq_inserted_at" && google_bigquery_table.pipeline["synthetic_rag/rag_chunks"].time_partitioning[0].field == "created_at" && length(google_bigquery_table.pipeline["synthetic_data_quality/fk_fanout_stats"].time_partitioning) == 0
    error_message = "Pipeline tables must follow docs/DEPLOYMENT_PREREQUISITES.md partitioning."
  }
  assert {
    condition     = strcontains(local.thelook_sql, "SELECT * REPLACE (CAST(NULL AS GEOGRAPHY) AS user_geom)") && !strcontains(local.thelook_sql, "EXCEPT")
    error_message = "Source snapshots keep the public schemas; only GEOGRAPHY values are nulled."
  }
  assert {
    condition = alltrue([for t in ["users", "orders", "order_items"] :
      strcontains(local.thelook_sql, "CREATE TABLE IF NOT EXISTS `synthetic-test-project.synthetic_data.${t}`\nLIKE `bigquery-public-data.thelook_ecommerce.${t}`;")
    ])
    error_message = "Landing tables must have the public tables' names and schemas, in synthetic_data."
  }
  assert {
    condition     = strcontains(local.thelook_sql, "CREATE OR REPLACE TABLE `synthetic-test-project.synthetic_data.products`")
    error_message = "The products catalog must land in the landing dataset: external FK parents resolve there."
  }
  assert {
    condition     = local.model_paths[var.model] == "gemma4/e4b-it/v1" && var.model_source == "huggingface" && local.machine_type == "g2-standard-8" && local.launch_parameters.vllm_dtype == "auto"
    error_message = "Gemma 4 E4B-it from Hugging Face on an L4 (G2, dtype auto) is the default."
  }
  assert {
    condition     = length(module.buckets) == 0 && length(google_compute_subnetwork_iam_member.dataflow_network_user) == 0 && length(google_dataflow_flex_template_job.generation) == 0
    error_message = "Existing bucket, default network and no Terraform launch are the defaults."
  }
  assert {
    condition     = toset([for m in regexall("\n  \"([a-z_]+)=", file("${local.pipeline_dir}/scripts/04_run_dataflow.sh")) : m[0]]) == toset(concat(keys(local.launch_parameters), ["num_rows", "run_id"]))
    error_message = "scripts/04_run_dataflow.sh and dataflow.tf must pass the same launch parameters."
  }
  assert {
    condition     = local.launch_parameters.create_if_not_exists == "false" && local.launch_parameters.relationships_uri == "config/relationships/gcp_public_fk_example.yaml"
    error_message = "The launch writes into the Terraform-created tables and generates the thelook model."
  }
}

run "terraform_launch" {
  command = plan

  variables {
    launch_job = true
  }

  assert {
    condition     = length(google_dataflow_flex_template_job.generation) == 1 && google_dataflow_flex_template_job.generation[0].ip_configuration == "WORKER_IP_PRIVATE" && google_dataflow_flex_template_job.generation[0].container_spec_gcs_path == local.template_path
    error_message = "launch_job = true must launch the Flex Template on private worker IPs."
  }
}

run "t4_profile" {
  command = plan

  variables {
    gpu   = "t4"
    model = "qwen3-4b"
  }

  assert {
    condition     = local.machine_type == "n1-standard-8" && strcontains(local.gpu.accelerator, "nvidia-tesla-t4") && local.launch_parameters.vllm_dtype == "float16"
    error_message = "A T4 runs on N1 and serves Qwen downcast to float16."
  }
}

run "gemma_needs_an_l4" {
  command = plan

  variables {
    gpu = "t4"
  }

  expect_failures = [local_file.variables_script]
}

run "gpu_fixes_the_machine_family" {
  command = plan

  variables {
    machine_type = "n1-standard-8"
  }

  expect_failures = [local_file.variables_script]
}

run "subnetwork_and_bucket_options" {
  command = plan

  variables {
    subnetwork    = "https://www.googleapis.com/compute/v1/projects/host-project/regions/us-central1/subnetworks/shared"
    create_bucket = true
    bucket_name   = "synthetic-test-bucket"
    model         = "qwen3-4b"
    model_source  = "modelscope"
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
