#  Copyright 2024 Google LLC
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
  dataflow_service_account = "dataflow-svc-account@learnings-421714.iam.gserviceaccount.com"
  bigtable_instance        = "iot-analytics"
  bigtable_zone            = "${var.region}-a"
  bigtable_lookup_key      = "vehicle_id"
  bigquery_dataset         = "iot"
  bigquery_table           = "maintenance_analytics"
}


// Project
module "google_cloud_project" {
  source          = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/project?ref=v38.0.0"
  billing_account = var.billing_account
  project_reuse   = var.project_create ? null : {}
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

resource "google_bigtable_instance" "iot-analytics" {
  name    = local.bigtable_instance
  project = var.project_id
  cluster {
    cluster_id   = "${local.bigtable_instance}-c1"
    num_nodes    = 1
    storage_type = "HDD"
    zone         = local.bigtable_zone
  }
}

# Create BigQuery dataset
resource "google_bigquery_dataset" "iot_analytics" {
  project     = var.project_id
  dataset_id  = local.bigquery_dataset
  description = "Dataset for storing clickstream analytics data"
  location    = var.region
}

# Create BigQuery table
resource "google_bigquery_table" "maintenance_analytics" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.iot_analytics.dataset_id
  table_id            = local.bigquery_table
  deletion_protection = false

  schema = jsonencode([
    { "name" : "vehicle_id", "type" : "STRING" },
    { "name" : "max_temperature", "type" : "INTEGER" },
    { "name" : "max_vibration", "type" : "FLOAT" },
    { "name" : "latest_timestamp", "type" : "TIMESTAMP" },
    { "name" : "last_service_date", "type" : "STRING" },
    { "name" : "maintenance_type", "type" : "STRING" },
    { "name" : "model", "type" : "STRING" },
    { "name" : "needs_maintenance", "type" : "INTEGER" }
  ])
}


// Buckets for staging data, scripts, etc, in the two regions
module "buckets" {
  source        = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/gcs?ref=v38.0.0"
  project_id    = module.google_cloud_project.project_id
  name          = module.google_cloud_project.project_id
  location      = var.region
  storage_class = "STANDARD"
  force_destroy = var.destroy_all_resources
}

module "input_topic" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/pubsub?ref=v38.0.0"
  project_id = module.google_cloud_project.project_id
  name       = var.pubsub_topic
  subscriptions = {
    messages-sub = {}
  }
}

// Service account
module "dataflow_sa" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v38.0.0"
  project_id = module.google_cloud_project.project_id
  name       = local.dataflow_service_account
  iam_project_roles = {
    (module.google_cloud_project.project_id) = [
      "roles/storage.admin",
      "roles/dataflow.worker",
      "roles/monitoring.metricWriter",
      "roles/pubsub.editor",
      "roles/bigtable.reader",
      "roles/bigquery.dataEditor"
    ]
  }
}

// Network
module "vpc_network" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/net-vpc?ref=v38.0.0"
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
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/net-vpc-firewall?ref=v38.0.0"
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
  filename        = "${path.module}/../../pipelines/iot_analytics_pipeline/scripts/00_set_environment.sh"
  file_permission = "0644"
  content         = <<FILE
# This file is generated by the Terraform code of this Solution Guide.
# We recommend that you modify this file only through the Terraform deployment.
export PROJECT_ID=${module.google_cloud_project.project_id}
export REGION=${var.region}
export CONTAINER_URI=gcr.io/$PROJECT_ID/iot-analytics:latest
export BIGTABLE_INSTANCE_ID=${google_bigtable_instance.iot-analytics.id}
export BIGTABLE_TABLE_ID=maintenance_data
export VEHICLE_DATA_PATH=../pipelines/iot_analytics/scripts/vehicle_data.jsonl
export MAINTENANCE_DATA_PATH=../pipelines/iot_analytics/scripts/maintenance_data.jsonl
export PUBSUB_TOPIC_ID=${var.pubsub_topic}
export MODEL_FILE_PATH=../pipelines/iot_analytics/maintenance_model.pkl
export SERVICE_ACCOUNT=${module.dataflow_sa.email}
export SUBNETWORK=regions/${var.region}/subnetworks/${var.network_prefix}-subnet
export MAX_DATAFLOW_WORKERS=3
export TEMP_LOCATION=gs://$PROJECT_ID/tmp
export TOPIC_ID=projects/$PROJECT_ID/topics/$PUBSUB_TOPIC_ID
export DATASET=${google_bigquery_dataset.iot_analytics.dataset_id}
export TABLE=${google_bigquery_table.maintenance_analytics.table_id}
export ROW_KEY=${local.bigtable_lookup_key}
FILE
}
