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
  dataflow_service_account = var.service_account_name != null ? var.service_account_name : "iot-analytics-sa"
  pubsub_topic             = var.pubsub_topic != null ? var.pubsub_topic : "maintenance-data"
  pubsub_subscription      = "${local.pubsub_topic}-sub"
  bigtable_instance        = "iot-analytics"
  bigtable_zone            = "${var.region}-a"
  bigtable_table           = "maintenance_data"
  bigtable_lookup_key      = "vehicle_id"
  bigquery_dataset         = "iot"
  bigquery_table           = "maintenance_analytics"
  bucket_name              = var.bucket_name != null ? var.bucket_name : var.project_id
  max_dataflow_workers     = 3
}

data "google_project" "project" {
  project_id = var.project_id
}

// Enable required Google Cloud APIs
resource "google_project_service" "dataflow" {
  project            = var.project_id
  service            = "dataflow.googleapis.com"
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

resource "google_project_service" "bigtable" {
  project            = var.project_id
  service            = "bigtableadmin.googleapis.com"
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


resource "google_project_service" "monitoring" {
  project            = var.project_id
  service            = "monitoring.googleapis.com"
  disable_on_destroy = false
}

// Artifact Registry repository for custom Dataflow worker containers
module "registry_docker" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/artifact-registry?ref=v58.0.0"
  project_id = var.project_id
  location   = var.region
  name       = "dataflow-containers"
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

// Cloud Bigtable instance for real-time IoT vehicle metadata enrichment
resource "google_bigtable_instance" "iot_analytics" {
  name                = local.bigtable_instance
  project             = var.project_id
  deletion_protection = !var.destroy_all_resources

  cluster {
    cluster_id   = "${local.bigtable_instance}-c1"
    num_nodes    = 1
    storage_type = "HDD"
    zone         = local.bigtable_zone
  }

  depends_on = [
    google_project_service.bigtable
  ]
}

// Cloud Bigtable table for maintenance data enrichment
resource "google_bigtable_table" "maintenance_data" {
  name                = local.bigtable_table
  instance_name       = google_bigtable_instance.iot_analytics.name
  project             = var.project_id
  deletion_protection = var.destroy_all_resources ? "UNPROTECTED" : "PROTECTED"

  column_family {
    family = "maintenance"
  }

  depends_on = [
    google_bigtable_instance.iot_analytics
  ]
}

// BigQuery dataset for processed IoT maintenance analytics
module "dataset" {
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

// BigQuery destination table for IoT maintenance analytics predictions
resource "google_bigquery_table" "maintenance_analytics" {
  project             = var.project_id
  dataset_id          = module.dataset.dataset_id
  table_id            = local.bigquery_table
  deletion_protection = !var.destroy_all_resources

  schema = jsonencode([
    { name = "vehicle_id", type = "STRING", mode = "NULLABLE" },
    { name = "max_temperature", type = "INTEGER", mode = "NULLABLE" },
    { name = "max_vibration", type = "FLOAT", mode = "NULLABLE" },
    { name = "latest_timestamp", type = "TIMESTAMP", mode = "NULLABLE" },
    { name = "last_service_date", type = "STRING", mode = "NULLABLE" },
    { name = "maintenance_type", type = "STRING", mode = "NULLABLE" },
    { name = "model", type = "STRING", mode = "NULLABLE" },
    { name = "needs_maintenance", type = "INTEGER", mode = "NULLABLE" }
  ])
}

// Optional GCS Bucket for staging & temp location
module "buckets" {
  count         = var.create_bucket ? 1 : 0
  source        = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/gcs?ref=v58.0.0"
  project_id    = var.project_id
  name          = local.bucket_name
  location      = var.region
  storage_class = "STANDARD"
  force_destroy = var.destroy_all_resources
}

// Pub/Sub input topic and subscription
module "input_topic" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/pubsub?ref=v58.0.0"
  project_id = var.project_id
  name       = local.pubsub_topic
  subscriptions = {
    (local.pubsub_subscription) = {}
  }

  depends_on = [
    google_project_service.pubsub
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
      "roles/bigtable.reader",
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
  filename        = "${path.module}/../../pipelines/iot_analytics/scripts/00_set_environment.sh"
  file_permission = "0644"
  content         = <<FILE
# This file is generated by the Terraform code of this Solution Guide.
# We recommend that you modify this file only through the Terraform deployment.
export PROJECT_ID=${var.project_id}
export REGION=${var.region}
export SUBNETWORK=${var.subnetwork != null ? var.subnetwork : ""}
export NETWORK=$${SUBNETWORK}
export TEMP_LOCATION=gs://${local.bucket_name}/tmp
export SERVICE_ACCOUNT=${module.dataflow_sa.email}

export DOCKER_REPOSITORY=${module.registry_docker.name}
export IMAGE_NAME=iot-analytics
export DOCKER_TAG=latest
export CONTAINER_URI=$REGION-docker.pkg.dev/$PROJECT_ID/$DOCKER_REPOSITORY/$IMAGE_NAME:$DOCKER_TAG

export BIGTABLE_INSTANCE_ID=${google_bigtable_instance.iot_analytics.name}
export INSTANCE_ID=$BIGTABLE_INSTANCE_ID
export BIGTABLE_TABLE_ID=${google_bigtable_table.maintenance_data.name}
export ROW_KEY=${local.bigtable_lookup_key}

export PUBSUB_TOPIC_ID=${local.pubsub_topic}
export TOPIC_ID=projects/$PROJECT_ID/topics/$PUBSUB_TOPIC_ID

export DATASET=${module.dataset.dataset_id}
export TABLE=${google_bigquery_table.maintenance_analytics.table_id}

SCRIPT_DIR="$(cd "$(dirname "$${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
export VEHICLE_DATA_PATH="$PIPELINE_DIR/scripts/vehicle_data.jsonl"
export MAINTENANCE_DATA_PATH="$PIPELINE_DIR/scripts/maintenance_data.jsonl"
export MODEL_FILE_PATH="$PIPELINE_DIR/maintenance_model.pkl"

export MAX_DATAFLOW_WORKERS=${local.max_dataflow_workers}
FILE
}
