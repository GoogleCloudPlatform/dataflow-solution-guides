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
  spanner_instance         = "test-spanner-instance"
  spanner_database         = "taxis_database"
  spanner_table            = "events"
  spanner_change_stream    = "events_stream"
  spanner_metadata_db      = "metadata"
  spanner_configuration    = "regional-${var.region}"
  spanner_name             = "Spanner instance managed by TF"
  bigquery_dataset         = "replica"
  dataflow_service_account = "my-dataflow-sa"
  worker_type              = "n2-standard-4"
  max_dataflow_workers     = 10
}

resource "google_project_service" "crm" {
  project                    = var.project_id
  service                    = "cloudresourcemanager.googleapis.com"
  disable_dependent_services = true
}

// Project
module "google_cloud_project" {
  depends_on      = [google_project_service.crm]
  source          = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/project?ref=v36.0.1"
  billing_account = var.billing_account
  project_create  = var.project_create
  name            = var.project_id
  parent          = var.organization
  services = [
    "iam.googleapis.com",
    "dataflow.googleapis.com",
    "monitoring.googleapis.com",
    "pubsub.googleapis.com",
    "autoscaling.googleapis.com",
    "spanner.googleapis.com",
    "bigquery.googleapis.com"
  ]
  service_config = {
    disable_on_destroy         = true
    disable_dependent_services = true
  }
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

// BigQuery dataset for final destination
module "dataset" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/bigquery-dataset?ref=v36.0.1"
  project_id = module.google_cloud_project.project_id
  id         = local.bigquery_dataset
  access = {
    dataflow-writer = { role = "OWNER", type = "user" }
  }
  access_identities = {
    dataflow-writer = module.dataflow_sa.email
  }

  options = {
    delete_contents_on_destroy = true
  }
}

// Spanner instance for change streams / CDC, using minimal instances size for demo purposes
resource "google_spanner_instance" "spanner_instance" {
  config           = local.spanner_configuration
  name             = local.spanner_instance
  project          = module.google_cloud_project.project_id
  display_name     = local.spanner_name
  processing_units = 1000
  force_destroy    = var.destroy_all_resources
}

resource "google_spanner_database" "taxis" {
  instance = google_spanner_instance.spanner_instance.name
  project  = module.google_cloud_project.project_id
  name     = local.spanner_database
  ddl = [
    <<DDL1
CREATE TABLE ${local.spanner_table} (
  ride_id STRING(64),
  point_idx INT64,
  latitude FLOAT64,
  longitude FLOAT64,
  timestamp TIMESTAMP,
  meter_reading FLOAT64,
  meter_increment FLOAT64,
  ride_status STRING(64),
  passenger_count INT64,
) PRIMARY KEY(ride_id, point_idx)
DDL1
    ,
    <<DDL2
CREATE CHANGE STREAM ${local.spanner_change_stream} FOR ${local.spanner_table}
OPTIONS(value_capture_type = 'NEW_ROW_AND_OLD_VALUES')
DDL2
  ]
  deletion_protection = !var.destroy_all_resources
}

resource "google_spanner_database_iam_binding" "read_write_taxis" {
  project  = module.google_cloud_project.project_id
  instance = google_spanner_instance.spanner_instance.name
  database = google_spanner_database.taxis.name
  role     = "roles/spanner.databaseUser"
  members = [
    module.dataflow_sa.iam_email
  ]
}

resource "google_spanner_database" "metadata" {
  instance            = google_spanner_instance.spanner_instance.name
  project             = module.google_cloud_project.project_id
  name                = local.spanner_metadata_db
  deletion_protection = !var.destroy_all_resources
}

resource "google_spanner_database_iam_binding" "read_write_metadata" {
  project  = module.google_cloud_project.project_id
  instance = google_spanner_instance.spanner_instance.name
  database = google_spanner_database.metadata.name
  role     = "roles/spanner.databaseUser"
  members = [
    module.dataflow_sa.iam_email
  ]
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
      "roles/pubsub.editor"
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

// So we can get to Internet if necessary (from the Dataflow region)
module "regional_nat" {
  count          = var.internet_access ? 1 : 0
  source         = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/net-cloudnat?ref=v36.0.1"
  project_id     = module.google_cloud_project.project_id
  region         = var.region
  name           = "${var.network_prefix}-nat"
  router_network = module.vpc_network.self_link
}

// Script with variables to launch the Dataflow jobs
resource "local_file" "variables_script" {
  filename        = "${path.module}/../../pipelines/etl_integration_java/scripts/01_set_variables.sh"
  file_permission = "0644"
  content         = <<FILE
# This file is generated by the Terraform code of this Solution Guide.
# We recommend that you modify this file only through the Terraform deployment.
export PROJECT=${module.google_cloud_project.project_id}
export REGION=${var.region}
export NETWORK=regions/${var.region}/subnetworks/${var.network_prefix}-subnet
export TEMP_LOCATION=gs://$PROJECT/tmp
export SERVICE_ACCOUNT=${module.dataflow_sa.email}

export TOPIC=projects/pubsub-public-data/topics/taxirides-realtime
export SPANNER_INSTANCE=${google_spanner_instance.spanner_instance.name}
export SPANNER_DATABASE=${local.spanner_database}
export SPANNER_METADATA_DB=${local.spanner_metadata_db}
export SPANNER_TABLE=${local.spanner_table}
export SPANNER_CHANGE_STREAM=${local.spanner_change_stream}

export BIGQUERY_DATASET=${local.bigquery_dataset}

export MAX_DATAFLOW_WORKERS=${local.max_dataflow_workers}
export WORKER_TYPE=${local.worker_type}
FILE
}
