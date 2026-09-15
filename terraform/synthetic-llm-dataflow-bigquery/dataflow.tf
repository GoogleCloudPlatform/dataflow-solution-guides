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

locals {
  # The launch parameters of scripts/04_run_dataflow.sh, minus run_id and
  # num_rows. Targeting order_items expands the launch to the whole model in
  # config/relationships/gcp_public_fk_example.yaml.
  launch_parameters = {
    reference_table       = "${local.source_fqn}.order_items"
    landing_table         = "${local.landing_fqn}.order_items"
    dlq_table             = "${var.project_id}.${local.quality_dataset}.dlq"
    validation_runs_table = "${var.project_id}.${local.quality_dataset}.validation_runs"
    fk_fanout_stats_table = "${var.project_id}.${local.quality_dataset}.fk_fanout_stats"
    rag_chunks_table      = "${var.project_id}.${local.rag_dataset}.rag_chunks"
    build_rag_layer       = "true"
    freetext_pools_table  = "${var.project_id}.${local.rag_dataset}.freetext_pools"
    build_pool_layer      = "true"
    source_stats_table    = "${var.project_id}.${local.rag_dataset}.source_table_stats"
    relationships_uri     = local.relationships_uri
    engine                = "b1_rag"
    client_type           = "vllm"
    model_uri             = local.model_uri
    embedder_uri          = local.embedder_uri
    vllm_dtype            = local.gpu.vllm_dtype
    vllm_max_model_len    = "8192"
    reference_rows_limit  = "10000"
    create_if_not_exists  = "false"
    write_disposition     = "overwrite"
    env                   = "dev"
  }
}

// A new run id (and job name) whenever what the job generates changes.
resource "random_id" "launch" {
  count       = var.launch_job ? 1 : 0
  byte_length = 3
  keepers = {
    template   = local.template_path
    num_rows   = var.num_rows
    model      = var.model
    machine    = local.machine_type
    parameters = sha256(jsonencode(local.launch_parameters))
  }
}

// Optional: the same launch as scripts/04_run_dataflow.sh, from Terraform
resource "google_dataflow_flex_template_job" "generation" {
  count                   = var.launch_job ? 1 : 0
  provider                = google-beta
  project                 = var.project_id
  region                  = var.region
  name                    = "sdfb-thelook-tf-${random_id.launch[0].hex}"
  container_spec_gcs_path = local.template_path
  service_account_email   = module.dataflow_sa.email
  temp_location           = "gs://${local.bucket_name}/tmp"
  staging_location        = "gs://${local.bucket_name}/staging"
  subnetwork              = local.subnetwork != "" ? local.subnetwork : null
  ip_configuration        = "WORKER_IP_PRIVATE"
  machine_type            = local.machine_type
  num_workers             = 1
  max_workers             = local.max_dataflow_workers
  # One SDK process per GPU worker: every process would otherwise start its
  # own vLLM server on the single GPU (ADR 0034).
  additional_experiments = [
    "enable_portable_runner",
    "worker_accelerator=${local.gpu.accelerator}",
    "no_use_multiple_sdk_containers",
  ]
  parameters = merge(local.launch_parameters, {
    num_rows = tostring(var.num_rows)
    run_id   = "thelook-tf-${random_id.launch[0].hex}"
  })
  labels = {
    solution = local.pipeline_name
  }
  on_delete = "cancel"

  depends_on = [
    google_bigquery_job.thelook_tables,
    google_bigquery_table.pipeline,
    google_compute_subnetwork_iam_member.dataflow_network_user,
  ]
}
