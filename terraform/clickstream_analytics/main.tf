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
  dataflow_service_account = var.service_account_name != null ? var.service_account_name : "clickstream-dataflow-sa"
  pubsub_topic             = "dataflow-clickstream-input"
  pubsub_subscription      = "dataflow-clickstream-input-sub"
  bigtable_instance        = "clickstream-analytics"
  bigtable_zone            = "${var.region}-a"
  bigtable_lookup_key      = "bigtable-lookup-key"
  bigquery_dataset         = "clickstream_analytics"
  bigquery_table           = "wikipedia"
  bigquery_deadletter      = "deadletter"
  bucket_name              = var.bucket_name != null ? var.bucket_name : var.project_id
  worker_type              = "n2-standard-4"
  max_dataflow_workers     = 10
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

// Cloud Bigtable instance for real-time clickstream enrichment
resource "google_bigtable_instance" "clickstream_analytics" {
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

// BigQuery dataset for processed clickstream and deadletter data
module "dataset" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/bigquery-dataset?ref=v57.0.0"
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

// BigQuery destination table for processed wikipedia clickstream data
resource "google_bigquery_table" "wikipedia" {
  project             = var.project_id
  dataset_id          = module.dataset.dataset_id
  table_id            = local.bigquery_table
  deletion_protection = !var.destroy_all_resources

  schema = jsonencode([
    { name = "prev", type = "STRING", mode = "NULLABLE" },
    { name = "curr", type = "STRING", mode = "NULLABLE" },
    { name = "type", type = "STRING", mode = "NULLABLE" },
    { name = "n", type = "INTEGER", mode = "NULLABLE" },
  ])
}

// BigQuery dead-letter table for unparseable or failed records
resource "google_bigquery_table" "deadletter" {
  project             = var.project_id
  dataset_id          = module.dataset.dataset_id
  table_id            = local.bigquery_deadletter
  deletion_protection = !var.destroy_all_resources

  schema = jsonencode([
    { name = "timestamp", type = "TIMESTAMP", mode = "REQUIRED" },
    { name = "payloadString", type = "STRING", mode = "REQUIRED" },
    { name = "payloadBytes", type = "BYTES", mode = "REQUIRED" },
    {
      name = "attributes", type = "RECORD", mode = "REPEATED", fields = [
        { name = "key", type = "STRING", mode = "NULLABLE" },
        { name = "value", type = "STRING", mode = "NULLABLE" }
      ]
    },
    { name = "errorMessage", type = "STRING", mode = "NULLABLE" },
    { name = "stacktrace", type = "STRING", mode = "NULLABLE" }
  ])
}

// Optional GCS Bucket for staging & temp location
module "buckets" {
  count         = var.create_bucket ? 1 : 0
  source        = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/gcs?ref=v57.0.0"
  project_id    = var.project_id
  name          = local.bucket_name
  location      = var.region
  storage_class = "STANDARD"
  force_destroy = var.destroy_all_resources
}

// Pub/Sub input topic and subscription
module "input_topic" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/pubsub?ref=v57.0.0"
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
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v57.0.0"
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
  filename        = "${path.module}/../../pipelines/clickstream_analytics_java/scripts/00_set_variables.sh"
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

export BQ_DATASET=${module.dataset.dataset_id}
export BQ_TABLE=${google_bigquery_table.wikipedia.table_id}
export BQ_DEADLETTER_TABLE=${google_bigquery_table.deadletter.table_id}

export TOPIC=${module.input_topic.id}
export SUBSCRIPTION=${module.input_topic.subscriptions[local.pubsub_subscription].id}

export BIGTABLE_INSTANCE=${google_bigtable_instance.clickstream_analytics.name}
export BIGTABLE_TABLE=$BQ_TABLE
export BT_LOOKUP_KEY=${local.bigtable_lookup_key}

export MAX_DATAFLOW_WORKERS=${local.max_dataflow_workers}
export WORKER_TYPE=${local.worker_type}
FILE
}
