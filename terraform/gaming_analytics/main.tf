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

locals {
  dataflow_service_account = var.service_account_name
  bucket_name              = var.bucket_name != null ? var.bucket_name : var.project_id
  subnetwork               = var.subnetwork != null ? trimspace(var.subnetwork) : ""

  // The model runs in the Python SDK harness on the worker, through the
  // cross-language RunInference transform. scikit-learn is CPU only, so there
  // is no accelerator and no GPU machine type: see the pipeline README.
  //
  // N2 rather than N1: the older N1 family is not offered in the newer regions
  // (europe-southwest1, for one), where Dataflow rejects the job outright with
  // "Unable to get machine type information for machine type n1-standard-2".
  // N2 is available everywhere this guide is likely to be deployed.
  machine_type         = var.machine_type != null ? var.machine_type : "n2-standard-2"
  worker_disk_size_gb  = 50
  max_dataflow_workers = 3

  // Path of the pickled model inside the custom Python SDK harness container.
  // The container build writes it here and the pipeline reads it from here, so
  // the workers never download the artifact.
  model_path = "/opt/gaming_analytics/recommender.pkl"


  input_subscription  = "${var.input_topic}-sub"
  output_subscription = "${var.output_topic}-sub"
  error_topic         = "gaming-analytics-errors"
  error_subscription  = "gaming-analytics-errors-sub"

  bigtable_instance      = "gaming-analytics"
  bigtable_table         = "player_features"
  bigtable_column_family = "features"

  bigquery_dataset = "gaming_analytics"
  bigquery_table   = "player_recommendations"

  services = [
    "compute.googleapis.com",
    "iam.googleapis.com",
    "storage.googleapis.com",
    "dataflow.googleapis.com",
    "monitoring.googleapis.com",
    "pubsub.googleapis.com",
    "bigquery.googleapis.com",
    "bigtable.googleapis.com",
    "bigtableadmin.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
  ]
}

data "google_project" "project" {
  project_id = var.project_id
}

// Declarative enablement of every API required by the application resources.
resource "google_project_service" "application" {
  for_each           = toset(local.services)
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

// Artifact Registry repository hosting the custom Python SDK harness image
// that the pipeline's cross-language RunInference step runs in. The model is
// baked into that image at build time, so the registry is on the critical
// path for launching the pipeline.
module "registry_docker" {
  depends_on = [google_project_service.application]
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/artifact-registry?ref=v58.0.0"
  project_id = var.project_id
  location   = var.region
  name       = "gaming-analytics-containers"
  format     = { docker = { standard = {} } }
  iam = {
    "roles/artifactregistry.writer" = [
      "serviceAccount:${data.google_project.project.number}@cloudbuild.gserviceaccount.com",
      "serviceAccount:${data.google_project.project.number}-compute@developer.gserviceaccount.com"
    ]
    "roles/artifactregistry.reader" = [
      module.dataflow_sa.iam_email
    ]
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

// Optional GCS bucket for Dataflow temp/staging files and model artifacts.
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

// Input topic: raw gameplay events published by the gaming platform servers.
module "input_topic" {
  depends_on = [google_project_service.application]
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/pubsub?ref=v58.0.0"
  project_id = var.project_id
  name       = var.input_topic
  subscriptions = {
    (local.input_subscription) = {}
  }
}

// Output topic: buffers in-game recommendations sent back to the gaming servers.
module "output_topic" {
  depends_on = [google_project_service.application]
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/pubsub?ref=v58.0.0"
  project_id = var.project_id
  name       = var.output_topic
  subscriptions = {
    (local.output_subscription) = {}
  }
}

// Dead-letter topic for elements the pipeline cannot process.
module "error_topic" {
  depends_on = [google_project_service.application]
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/pubsub?ref=v58.0.0"
  project_id = var.project_id
  name       = local.error_topic
  subscriptions = {
    (local.error_subscription) = {}
  }
}

// Cloud Bigtable feature store backing the Apache Beam Enrichment transform.
module "feature_table" {
  depends_on          = [google_project_service.application]
  source              = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/bigtable-instance?ref=v58.0.0"
  project_id          = var.project_id
  name                = local.bigtable_instance
  deletion_protection = !var.destroy_all_resources
  clusters = {
    cluster1 = {
      zone      = "${var.region}-${var.zone}"
      num_nodes = 1
    }
  }
  tables = {
    player_features = {
      column_families = { features = {} }
    }
  }
}

// BigQuery dataset for the analytics side of the architecture.
module "output_dataset" {
  depends_on = [google_project_service.application]
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/bigquery-dataset?ref=v58.0.0"
  project_id = var.project_id
  id         = local.bigquery_dataset
  location   = var.region
  options    = { delete_contents_on_destroy = var.destroy_all_resources }
}

// Destination table with scored gameplay events, partitioned on event time.
resource "google_bigquery_table" "player_recommendations" {
  project             = var.project_id
  dataset_id          = module.output_dataset.dataset_id
  table_id            = local.bigquery_table
  deletion_protection = !var.destroy_all_resources

  time_partitioning {
    type  = "DAY"
    field = "event_timestamp"
  }
  clustering = ["player_id", "event_type"]

  schema = jsonencode([
    { name = "player_id", type = "STRING", mode = "REQUIRED" },
    { name = "session_id", type = "STRING", mode = "NULLABLE" },
    { name = "event_type", type = "STRING", mode = "NULLABLE" },
    { name = "level", type = "INTEGER", mode = "NULLABLE" },
    { name = "score", type = "INTEGER", mode = "NULLABLE" },
    { name = "recommendation", type = "STRING", mode = "NULLABLE" },
    { name = "recommendation_score", type = "FLOAT", mode = "NULLABLE" },
    { name = "event_timestamp", type = "TIMESTAMP", mode = "REQUIRED" },
    { name = "processing_timestamp", type = "TIMESTAMP", mode = "NULLABLE" }
  ])
}

// Dedicated Dataflow worker service account with least-privilege project roles.
module "dataflow_sa" {
  depends_on = [google_project_service.application]
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v58.0.0"
  project_id = var.project_id
  name       = local.dataflow_service_account
  iam_project_roles = {
    (var.project_id) = [
      "roles/dataflow.worker",
      "roles/monitoring.metricWriter",
      "roles/storage.objectAdmin",
      "roles/pubsub.editor",
      "roles/bigquery.dataEditor",
      "roles/bigquery.jobUser",
    ]
  }
}

// Feature lookups are read-only, and scoped to the single-purpose instance
// this module creates rather than to the whole project.
//
// This has to be an instance-scoped binding, not a table-scoped one. Reading
// rows only needs `bigtable.tables.readRows` on the table, but the Bigtable
// client also runs a channel-pool health checker that calls
// `bigtable.instances.ping`, and that permission is authorized against the
// *instance*. With a table-scoped binding the data reads succeed while every
// background probe fails with PERMISSION_DENIED, which floods the worker logs
// and makes the client recycle gRPC channels it believes are unhealthy.
resource "google_bigtable_instance_iam_member" "worker_features" {
  project  = var.project_id
  instance = local.bigtable_instance
  role     = "roles/bigtable.reader"
  member   = module.dataflow_sa.iam_email

  depends_on = [module.feature_table]
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

// Script with variables to launch the Dataflow jobs
resource "local_file" "variables_script" {
  filename        = "${path.module}/../../pipelines/gaming_analytics_java/scripts/00_set_environment.sh"
  file_permission = "0644"
  content         = <<FILE
# This file is generated by the Terraform code of this Solution Guide.
# We recommend that you modify this file only through the Terraform deployment.
export PROJECT=${var.project_id}
export REGION=${var.region}
export ZONE=${var.region}-${var.zone}
export SUBNETWORK=${local.subnetwork}
export NETWORK=$${SUBNETWORK}
export TEMP_LOCATION=gs://${local.bucket_name}/tmp
export SERVICE_ACCOUNT=${module.dataflow_sa.email}

export DOCKER_REPOSITORY=${module.registry_docker.name}
export IMAGE_NAME=dataflow-solutions-gaming-analytics
export DOCKER_TAG=0.1
export DOCKER_IMAGE=$REGION-docker.pkg.dev/$PROJECT/$DOCKER_REPOSITORY/$IMAGE_NAME
export CONTAINER_URI=$DOCKER_IMAGE:$DOCKER_TAG

# Where 01_build_and_push_container.sh bakes the scikit-learn model inside the
# harness image, and where the pipeline reads it from at run time. The two must
# agree, so both default to this single value.
export MODEL_PATH=${local.model_path}

export MACHINE_TYPE=${local.machine_type}
export DISK_SIZE_GB=${local.worker_disk_size_gb}
export MAX_DATAFLOW_WORKERS=${local.max_dataflow_workers}

export INPUT_TOPIC=projects/${var.project_id}/topics/${var.input_topic}
export INPUT_SUBSCRIPTION=projects/${var.project_id}/subscriptions/${local.input_subscription}
export OUTPUT_TOPIC=projects/${var.project_id}/topics/${var.output_topic}
export OUTPUT_SUBSCRIPTION=projects/${var.project_id}/subscriptions/${local.output_subscription}
export ERROR_TOPIC=projects/${var.project_id}/topics/${local.error_topic}
export ERROR_SUBSCRIPTION=projects/${var.project_id}/subscriptions/${local.error_subscription}

export BIGTABLE_INSTANCE=${local.bigtable_instance}
export BIGTABLE_TABLE=${local.bigtable_table}
export BIGTABLE_COLUMN_FAMILY=${local.bigtable_column_family}

export BQ_DATASET=${module.output_dataset.dataset_id}
export BQ_TABLE=${var.project_id}.${module.output_dataset.dataset_id}.${google_bigquery_table.player_recommendations.table_id}
export BUCKET=${local.bucket_name}
FILE
}
