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
  pipeline_name = "synthetic-llm-dataflow-bigquery"
  pipeline_dir  = "${path.module}/../../pipelines/${local.pipeline_name}"
  bucket_name   = var.bucket_name != null ? var.bucket_name : var.project_id
  subnetwork    = var.subnetwork != null ? trimspace(var.subnetwork) : ""

  # The image tag follows the synced golden-source ref (.sync-source.json).
  source_ref = try(jsondecode(file("${local.pipeline_dir}/.sync-source.json")).ref, "dev")
  docker_tag = replace(local.source_ref, "/[^A-Za-z0-9_.-]/", "-")

  template_path = "gs://${local.bucket_name}/templates/${local.pipeline_name}-${local.docker_tag}.json"

  # GCS layout written by scripts/02_stage_models.sh (docs/MODEL_LAYOUT.md).
  model_paths = {
    "gemma4-e4b-it" = "gemma4/e4b-it/v1"
    "qwen3-4b"      = "qwen3/4b-instruct-2507/v1"
  }
  models_prefix = "gs://${local.bucket_name}/synthetic/models"
  model_uri     = "${local.models_prefix}/${local.model_paths[var.model]}/"
  embedder_uri  = "${local.models_prefix}/embedders/bge-small-en-v1.5/v1/"

  source_dataset  = "synthetic_source"
  landing_dataset = "synthetic_data"
  quality_dataset = "synthetic_data_quality"
  rag_dataset     = "synthetic_rag"

  relationships_uri    = "config/relationships/gcp_public_fk_example.yaml"
  max_dataflow_workers = 1

  # The GPU fixes the machine family and the vLLM dtype. bf16 needs compute
  # capability 8.0: an L4 (8.9, 24 GB) serves the bf16 checkpoints as they
  # are, while a T4 (7.5, 16 GB) serves Qwen downcast to fp16 and cannot run
  # Gemma, whose fp16 activations overflow (config/models.yml). The T4 driver
  # pin follows Dataflow's vLLM guidance (docs/RUN_PLAYBOOK.md).
  gpu_profiles = {
    l4 = {
      machine_family = "g2"
      machine_type   = "g2-standard-8"
      accelerator    = "type:nvidia-l4;count:1;install-nvidia-driver"
      vllm_dtype     = "auto"
      models         = ["gemma4-e4b-it", "qwen3-4b"]
    }
    t4 = {
      machine_family = "n1"
      machine_type   = "n1-standard-8"
      accelerator    = "type:nvidia-tesla-t4;count:1;install-nvidia-driver:5xx"
      vllm_dtype     = "float16"
      models         = ["qwen3-4b"]
    }
  }
  gpu          = local.gpu_profiles[var.gpu]
  machine_type = coalesce(var.machine_type, local.gpu.machine_type)
}

data "google_project" "project" {
  project_id = var.project_id
}

resource "google_project_service" "application" {
  for_each = toset([
    "artifactregistry.googleapis.com", "bigquery.googleapis.com", "bigquerystorage.googleapis.com",
    "cloudbuild.googleapis.com", "compute.googleapis.com", "dataflow.googleapis.com",
    "iam.googleapis.com", "logging.googleapis.com", "monitoring.googleapis.com",
    "storage.googleapis.com",
  ])
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

// Artifact Registry repository for the launcher + GPU worker image
module "registry_docker" {
  depends_on = [google_project_service.application]
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/artifact-registry?ref=v58.0.0"
  project_id = var.project_id
  location   = var.region
  name       = "synthetic-llm-containers"
  format     = { docker = { standard = {} } }
  iam = {
    "roles/artifactregistry.writer" = [module.build_sa.iam_email]
    "roles/artifactregistry.reader" = [module.dataflow_sa.iam_email]
  }
  cleanup_policy_dry_run = false
  cleanup_policies = {
    keep-3-versions = {
      action = "KEEP"
      most_recent_versions = {
        keep_count = 3
      }
    }
  }
}

// Optional bucket for model weights, Dataflow temp/staging and the template spec
module "buckets" {
  depends_on    = [google_project_service.application]
  count         = var.create_bucket ? 1 : 0
  source        = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/gcs?ref=v58.0.0"
  project_id    = var.project_id
  name          = local.bucket_name
  location      = var.region
  storage_class = "STANDARD"
  force_destroy = var.destroy_all_resources
}

// Dedicated identity for the Flex Template launcher and the Dataflow workers
module "dataflow_sa" {
  depends_on = [google_project_service.application]
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v58.0.0"
  project_id = var.project_id
  name       = var.service_account_name
  iam_project_roles = {
    (var.project_id) = [
      "roles/dataflow.worker",
      "roles/dataflow.developer",
      "roles/bigquery.dataEditor",
      "roles/bigquery.jobUser",
      "roles/bigquery.readSessionUser",
      "roles/storage.objectAdmin",
      "roles/monitoring.metricWriter",
      "roles/logging.logWriter",
    ]
  }
}

// Dedicated identity for Cloud Build (image build and model staging)
module "build_sa" {
  depends_on = [google_project_service.application]
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v58.0.0"
  project_id = var.project_id
  name       = var.build_service_account_name
  iam_project_roles = {
    (var.project_id) = [
      "roles/logging.logWriter",
      "roles/storage.objectAdmin",
    ]
  }
}

// Additive permission on the existing local or Shared VPC subnet.
resource "google_compute_subnetwork_iam_member" "dataflow_network_user" {
  count      = local.subnetwork != "" ? 1 : 0
  project    = try(regex("projects/([^/]+)/", local.subnetwork)[0], var.project_id)
  region     = try(regex("regions/([^/]+)/", local.subnetwork)[0], var.region)
  subnetwork = try(regex("subnetworks/([^/]+)", local.subnetwork)[0], local.subnetwork)
  role       = "roles/compute.networkUser"
  member     = module.dataflow_sa.iam_email
  depends_on = [google_project_service.application]
}

// Script with variables to build, stage, launch and verify
resource "local_file" "variables_script" {
  filename        = "${local.pipeline_dir}/scripts/00_set_variables.sh"
  file_permission = "0644"
  content         = <<FILE
# This file is generated by the Terraform code of this Solution Guide.
# We recommend that you modify this file only through the Terraform deployment.
export PROJECT=${var.project_id}
export REGION=${var.region}
export BQ_LOCATION=${var.bq_location}
export SUBNETWORK=${local.subnetwork}
export NETWORK=$${SUBNETWORK}
export BUCKET=${local.bucket_name}
export TEMP_LOCATION=gs://${local.bucket_name}/tmp
export STAGING_LOCATION=gs://${local.bucket_name}/staging
export SERVICE_ACCOUNT=${module.dataflow_sa.email}
export BUILD_SERVICE_ACCOUNT=${module.build_sa.email}

export DOCKER_REPOSITORY=${module.registry_docker.name}
export IMAGE_NAME=${local.pipeline_name}
export DOCKER_TAG=${local.docker_tag}
export DOCKER_IMAGE=$REGION-docker.pkg.dev/$PROJECT/$DOCKER_REPOSITORY/$IMAGE_NAME
export CONTAINER_URI=$DOCKER_IMAGE:$DOCKER_TAG
export TEMPLATE_PATH=${local.template_path}

export MODEL=${var.model}
export MODEL_SOURCE=${var.model_source}
export MODEL_URI=${local.model_uri}
export EMBEDDER_URI=${local.embedder_uri}

export SOURCE_DATASET=${local.source_dataset}
export LANDING_DATASET=${local.landing_dataset}
export QUALITY_DATASET=${local.quality_dataset}
export RAG_DATASET=${local.rag_dataset}
export RELATIONSHIPS_URI=${local.relationships_uri}
export NUM_ROWS=${var.num_rows}

export MAX_DATAFLOW_WORKERS=${local.max_dataflow_workers}
export GPU=${var.gpu}
export MACHINE_TYPE=${local.machine_type}
export ACCELERATOR="${local.gpu.accelerator}"
export VLLM_DTYPE=${local.gpu.vllm_dtype}
FILE

  lifecycle {
    precondition {
      condition     = contains(local.gpu.models, var.model)
      error_message = "model ${var.model} does not run on gpu ${var.gpu}: Gemma needs bf16 (compute capability 8.0+, an L4). Use gpu = \"l4\" or model = \"qwen3-4b\"."
    }
    precondition {
      condition     = startswith(local.machine_type, "${local.gpu.machine_family}-")
      error_message = "gpu ${var.gpu} attaches only to ${local.gpu.machine_family}-* machine types, not ${local.machine_type}."
    }
  }
}
