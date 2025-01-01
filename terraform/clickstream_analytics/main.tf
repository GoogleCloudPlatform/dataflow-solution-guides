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
  dataflow_service_account = "my-dataflow-sa"
  bigtable_instance        = "clickstream-analytics"
  bigtable_zone            = "${var.region}-a"
  bigtable_lookup_key      = "bigtable-lookup-key"
  bigquery_dataset         = "clickstream_analytics"
}


// Project
module "google_cloud_project" {
  source          = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/project?ref=v36.0.1"
  billing_account = var.billing_account
  project_create  = var.project_create
  name            = var.project_id
  parent          = var.organization
  services = [
    "dataflow.googleapis.com",
    "monitoring.googleapis.com",
    "pubsub.googleapis.com",
    "autoscaling.googleapis.com",
    "bigtableadmin.googleapis.com",
    "bigquery.googleapis.com"
  ]
}

resource "google_bigtable_instance" "clickstream-analytics" {
  name = local.bigtable_instance

  cluster {
    cluster_id   = "${local.bigtable_instance}-c1"
    num_nodes    = 1
    storage_type = "HDD"
    zone         = local.bigtable_zone
  }
}

# Create BigQuery dataset
resource "google_bigquery_dataset" "clickstream_analytics" {
  dataset_id  = local.bigquery_dataset
  description = "Dataset for storing clickstream analytics data"
  location    = var.region
}

# Create BigQuery table
resource "google_bigquery_table" "wikipedia" {
  dataset_id          = google_bigquery_dataset.clickstream_analytics.dataset_id
  table_id            = "wikipedia"
  deletion_protection = false

  schema = jsonencode([
    { name = "prev", type = "STRING", mode = "NULLABLE" },
    { name = "curr", type = "STRING", mode = "NULLABLE" },
    { name = "type", type = "STRING", mode = "NULLABLE" },
    { name = "n", type = "INTEGER", mode = "NULLABLE" },
  ])
}

resource "google_bigquery_table" "deadletter" {
  dataset_id          = google_bigquery_dataset.clickstream_analytics.dataset_id
  table_id            = "deadletter"
  deletion_protection = false

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

// Buckets for staging data, scripts, etc, in the two regions
module "buckets" {
  source        = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/gcs?ref=v36.0.1"
  project_id    = module.google_cloud_project.project_id
  name          = module.google_cloud_project.project_id
  location      = var.region
  storage_class = "STANDARD"
  force_destroy = var.destroy_all_resources
}

module "input_topic" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/pubsub?ref=v36.0.1"
  project_id = module.google_cloud_project.project_id
  name       = "input"
  subscriptions = {
    messages-sub = {}
  }
}

// Service account
module "dataflow_sa" {
  source       = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v36.0.1"
  project_id   = module.google_cloud_project.project_id
  name         = local.dataflow_service_account
  generate_key = false
  iam_project_roles = {
    (module.google_cloud_project.project_id) = [
      "roles/storage.admin",
      "roles/dataflow.worker",
      "roles/monitoring.metricWriter",
      "roles/pubsub.editor",
      "roles/bigtable.reader"
    ]
  }
}

// Network
module "vpc_network" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/net-vpc?ref=v36.0.1"
  project_id = module.google_cloud_project.project_id
  name       = "${var.network_prefix}-net"
  subnets = [
    {
      ip_cidr_range         = "10.1.0.0/16"
      name                  = "${var.network_prefix}-subnet"
      region                = var.region
      enable_private_access = true
      secondary_ip_ranges = {
        pods     = "10.16.0.0/14"
        services = "10.20.0.0/24"
      }
    }
  ]
}

module "firewall_rules" {
  // Default rules for internal traffic + SSH access via IAP
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/net-vpc-firewall?ref=v36.0.1"
  project_id = module.google_cloud_project.project_id
  network    = module.vpc_network.name
  default_rules_config = {
    admin_ranges = [
      module.vpc_network.subnet_ips["${var.region}/${var.network_prefix}-subnet"],
    ]
  }
  egress_rules = {
    allow-egress-dataflow = {
      deny        = false
      description = "Dataflow firewall rule egress"
      targets     = ["dataflow"]
      rules       = [{ protocol = "tcp", ports = [12345, 12346] }]
    }
  }
  ingress_rules = {
    allow-ingress-dataflow = {
      description = "Dataflow firewall rule ingress"
      targets     = ["dataflow"]
      rules       = [{ protocol = "tcp", ports = [12345, 12346] }]
    }
  }
}

resource "local_file" "variables_script" {
  filename        = "${path.module}/../../pipelines/clickstream_analytics_java/scripts/00_set_variables.sh"
  file_permission = "0644"
  content         = <<FILE
# This file is generated by the Terraform code of this Solution Guide.
# We recommend that you modify this file only through the Terraform deployment.
export PROJECT=${module.google_cloud_project.project_id}
export REGION=${var.region}
export SUBNETWORK=regions/${var.region}/subnetworks/${var.network_prefix}-subnet
export TEMP_LOCATION=gs://$PROJECT/tmp
export SERVICE_ACCOUNT=${module.dataflow_sa.email}

export BQ_DATASET=${google_bigquery_dataset.clickstream_analytics.dataset_id}
export BQ_TABLE=${google_bigquery_table.wikipedia.table_id}
export BQ_DEADLETTER_TABLE=${google_bigquery_table.deadletter.table_id}

export SUBSCRIPTION=${module.input_topic.subscriptions["messages-sub"].id}

export BIGTABLE_INSTANCE=${google_bigtable_instance.clickstream-analytics.id}
export BIGTABLE_TABLE=$BQ_TABLE
export BT_LOOKUP_KEY=${local.bigtable_lookup_key}
FILE
}
