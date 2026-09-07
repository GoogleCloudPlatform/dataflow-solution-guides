#  Copyright 2025 Google LLC
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
  bucket_name               = var.bucket_name != null ? var.bucket_name : var.project_id
  dataflow_service_account  = var.service_account_name != null ? var.service_account_name : "cdp-dataflow-sa"
  max_dataflow_workers      = 1
  worker_disk_size_gb       = 200
  machine_type              = "e2-standard-8"
  bigquery_dataset          = var.bq_dataset
  bigquery_table            = var.bq_table
  bigquery_sessions_table   = var.bq_sessions_table
  bigquery_deadletter_table = var.bq_deadletter_table
  transactions_topic        = var.pubsub_transactions_topic
  transactions_sub          = "${var.pubsub_transactions_topic}-sub"
  coupon_redemption_topic   = var.pubsub_coupon_redemption_topic
  coupon_redemption_sub     = "${var.pubsub_coupon_redemption_topic}-sub"
  artifact_registry_repo    = var.artifact_registry_name
}

data "google_project" "project" {
  project_id = var.project_id
}

// Enable required Google Cloud APIs natively
resource "google_project_service" "dataflow" {
  project            = var.project_id
  service            = "dataflow.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "cloudbuild" {
  project            = var.project_id
  service            = "cloudbuild.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "artifactregistry" {
  project            = var.project_id
  service            = "artifactregistry.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "pubsub" {
  project            = var.project_id
  service            = "pubsub.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "bigquery" {
  project            = var.project_id
  service            = "bigquery.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "monitoring" {
  project            = var.project_id
  service            = "monitoring.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "storage" {
  project            = var.project_id
  service            = "storage.googleapis.com"
  disable_on_destroy = false
}

// Artifact Registry repository for custom Dataflow worker containers
module "registry_docker" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/artifact-registry?ref=v58.0.0"
  project_id = var.project_id
  location   = var.region
  name       = local.artifact_registry_repo
  format     = { docker = { standard = {} } }
  iam = {
    "roles/artifactregistry.admin" = [
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

  depends_on = [
    google_project_service.artifactregistry
  ]
}

// Optional GCS Bucket for staging data and scripts
module "buckets" {
  count         = var.create_bucket ? 1 : 0
  source        = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/gcs?ref=v58.0.0"
  project_id    = var.project_id
  name          = local.bucket_name
  location      = var.region
  storage_class = "STANDARD"
  force_destroy = var.destroy_all_resources

  depends_on = [
    google_project_service.storage
  ]
}

// Pub/Sub transactions topic and subscription
module "transactions_topic" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/pubsub?ref=v58.0.0"
  project_id = var.project_id
  name       = local.transactions_topic
  subscriptions = {
    (local.transactions_sub) = {}
  }

  depends_on = [
    google_project_service.pubsub
  ]
}

// Pub/Sub coupon redemption topic and subscription
module "coupon_redemption_topic" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/pubsub?ref=v58.0.0"
  project_id = var.project_id
  name       = local.coupon_redemption_topic
  subscriptions = {
    (local.coupon_redemption_sub) = {}
  }

  depends_on = [
    google_project_service.pubsub
  ]
}

// BigQuery dataset for unified CDP data
module "cdp_dataset" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/bigquery-dataset?ref=v58.0.0"
  project_id = var.project_id
  id         = local.bigquery_dataset
  location   = var.region
  access = {
    dataflow-writer = { role = "OWNER", type = "user" }
  }
  access_identities = {
    dataflow-writer = module.dataflow_sa.email
  }
  options = {
    delete_contents_on_destroy = var.destroy_all_resources
  }

  depends_on = [
    google_project_service.bigquery
  ]
}

// BigQuery destination table for unified customer transactions and coupons
resource "google_bigquery_table" "unified_customer_data" {
  project             = var.project_id
  dataset_id          = module.cdp_dataset.dataset_id
  table_id            = local.bigquery_table
  deletion_protection = !var.destroy_all_resources

  schema = jsonencode([
    { name = "session_id", type = "STRING", mode = "REQUIRED" },
    { name = "transaction_id", type = "STRING", mode = "REQUIRED" },
    { name = "household_key", type = "STRING", mode = "REQUIRED" },
    { name = "product_id", type = "STRING", mode = "NULLABLE" },
    { name = "quantity", type = "INTEGER", mode = "NULLABLE" },
    { name = "sales_value", type = "FLOAT", mode = "NULLABLE" },
    { name = "store_id", type = "STRING", mode = "NULLABLE" },
    { name = "retail_disc", type = "FLOAT", mode = "NULLABLE" },
    { name = "coupon_discount", type = "FLOAT", mode = "NULLABLE" },
    { name = "coupon_match_disc", type = "FLOAT", mode = "NULLABLE" },
    { name = "coupon_upc", type = "STRING", mode = "NULLABLE" },
    { name = "campaign", type = "STRING", mode = "NULLABLE" },
    { name = "day", type = "INTEGER", mode = "NULLABLE" },
    { name = "trans_time", type = "STRING", mode = "NULLABLE" },
    { name = "week_no", type = "INTEGER", mode = "NULLABLE" },
    { name = "event_timestamp", type = "TIMESTAMP", mode = "NULLABLE" },
    { name = "processed_timestamp", type = "TIMESTAMP", mode = "REQUIRED" }
  ])

  depends_on = [
    module.cdp_dataset
  ]
}

// BigQuery destination table for sessionized Customer 360 profiles
resource "google_bigquery_table" "customer_sessions" {
  project             = var.project_id
  dataset_id          = module.cdp_dataset.dataset_id
  table_id            = local.bigquery_sessions_table
  deletion_protection = !var.destroy_all_resources

  schema = jsonencode([
    { name = "session_id", type = "STRING", mode = "REQUIRED" },
    { name = "household_key", type = "STRING", mode = "REQUIRED" },
    { name = "session_start", type = "TIMESTAMP", mode = "REQUIRED" },
    { name = "session_end", type = "TIMESTAMP", mode = "REQUIRED" },
    { name = "session_duration_sec", type = "INTEGER", mode = "REQUIRED" },
    { name = "total_transactions", type = "INTEGER", mode = "REQUIRED" },
    { name = "total_items_purchased", type = "INTEGER", mode = "REQUIRED" },
    { name = "total_spend", type = "FLOAT", mode = "REQUIRED" },
    { name = "total_discount", type = "FLOAT", mode = "REQUIRED" },
    { name = "coupons_redeemed_count", type = "INTEGER", mode = "REQUIRED" },
    { name = "distinct_products_count", type = "INTEGER", mode = "REQUIRED" },
    { name = "campaigns", type = "STRING", mode = "REPEATED" },
    { name = "stores_visited", type = "STRING", mode = "REPEATED" },
    { name = "processed_timestamp", type = "TIMESTAMP", mode = "REQUIRED" }
  ])

  depends_on = [
    module.cdp_dataset
  ]
}

// BigQuery dead-letter table for malformed or rejected records
resource "google_bigquery_table" "cdp_deadletter" {
  project             = var.project_id
  dataset_id          = module.cdp_dataset.dataset_id
  table_id            = local.bigquery_deadletter_table
  deletion_protection = !var.destroy_all_resources

  schema = jsonencode([
    { name = "source", type = "STRING", mode = "REQUIRED" },
    { name = "raw_payload", type = "STRING", mode = "NULLABLE" },
    { name = "error_message", type = "STRING", mode = "REQUIRED" },
    { name = "timestamp", type = "TIMESTAMP", mode = "REQUIRED" }
  ])

  depends_on = [
    module.cdp_dataset
  ]
}

// Dedicated Dataflow Worker Service Account
module "dataflow_sa" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v58.0.0"
  project_id = var.project_id
  name       = local.dataflow_service_account
  iam_project_roles = {
    (var.project_id) = [
      "roles/storage.objectAdmin",
      "roles/dataflow.worker",
      "roles/monitoring.metricWriter",
      "roles/pubsub.editor",
      "roles/bigquery.dataEditor",
      "roles/bigquery.jobUser"
    ]
  }
}

// Grant networkUser role on the subnetwork to the Dataflow worker service account (supports Shared VPC and local subnets)
resource "google_compute_subnetwork_iam_member" "dataflow_network_user" {
  count      = var.subnetwork != null ? 1 : 0
  project    = length(regexall("projects/([^/]+)/", var.subnetwork)) > 0 ? regex("projects/([^/]+)/", var.subnetwork)[0] : var.project_id
  region     = length(regexall("regions/([^/]+)/", var.subnetwork)) > 0 ? regex("regions/([^/]+)/", var.subnetwork)[0] : var.region
  subnetwork = length(regexall("subnetworks/([^/]+)", var.subnetwork)) > 0 ? regex("subnetworks/([^/]+)", var.subnetwork)[0] : var.subnetwork
  role       = "roles/compute.networkUser"
  member     = module.dataflow_sa.iam_email
}

// Script with variables to launch the Dataflow jobs
resource "local_file" "variables_script" {
  filename        = "${path.module}/../../pipelines/cdp/scripts/00_set_environment.sh"
  file_permission = "0644"
  content         = <<FILE
# This file is generated by the Terraform code of this Solution Guide.
# We recommend that you modify this file only through the Terraform deployment.
export PROJECT=${var.project_id}
export REGION=${var.region}
export SUBNETWORK=${var.subnetwork != null ? var.subnetwork : ""}
export NETWORK=$${SUBNETWORK}
export TEMP_LOCATION=gs://${local.bucket_name}/tmp
export SERVICE_ACCOUNT=${module.dataflow_sa.email}

export DOCKER_REPOSITORY=${module.registry_docker.name}
export IMAGE_NAME=dataflow-solutions-cdp
export DOCKER_TAG=0.1
export DOCKER_IMAGE=$REGION-docker.pkg.dev/$PROJECT/$DOCKER_REPOSITORY/$IMAGE_NAME

export CONTAINER_URI=$DOCKER_IMAGE:$DOCKER_TAG
export TRANSACTIONS_TOPIC=${module.transactions_topic.id}
export TRANSACTIONS_SUBSCRIPTION=projects/${var.project_id}/subscriptions/${local.transactions_sub}
export COUPON_REDEMPTION_TOPIC=${module.coupon_redemption_topic.id}
export COUPON_REDEMPTION_SUBSCRIPTION=projects/${var.project_id}/subscriptions/${local.coupon_redemption_sub}
export MAX_DATAFLOW_WORKERS=${local.max_dataflow_workers}
export DISK_SIZE_GB=${local.worker_disk_size_gb}
export MACHINE_TYPE=${local.machine_type}

export BQ_DATASET=${module.cdp_dataset.dataset_id}
export BQ_UNIFIED_TABLE=${google_bigquery_table.unified_customer_data.table_id}
export BQ_SESSIONS_TABLE=${google_bigquery_table.customer_sessions.table_id}
export BQ_DEADLETTER_TABLE=${google_bigquery_table.cdp_deadletter.table_id}
export GCS_BUCKET=gs://${local.bucket_name}/assets/dataflow-solution-guide-cdp
FILE
}

