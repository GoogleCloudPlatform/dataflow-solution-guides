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
  dataflow_service_account = var.service_account_name
  bucket_name              = var.bucket_name != null ? var.bucket_name : var.project_id
  subnetwork               = var.subnetwork != null ? trimspace(var.subnetwork) : ""
  max_dataflow_workers     = 1
  worker_disk_size_gb      = 200
  machine_type             = "n1-standard-2"
  bigquery_dataset         = "anomaly_detection"
  bigtable_instance        = "anomaly-detection"
}


data "google_project" "project" {
  project_id = var.project_id
}

resource "google_project_service" "application" {
  for_each = toset([
    "aiplatform.googleapis.com", "iam.googleapis.com", "compute.googleapis.com",
    "storage.googleapis.com", "dataflow.googleapis.com", "monitoring.googleapis.com",
    "cloudbuild.googleapis.com", "artifactregistry.googleapis.com", "pubsub.googleapis.com",
    "bigtable.googleapis.com", "bigtableadmin.googleapis.com", "bigquery.googleapis.com",
  ])
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

module "registry_docker" {
  depends_on = [google_project_service.application]
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/artifact-registry?ref=v58.0.0"
  project_id = var.project_id
  location   = var.region
  name       = "anomaly-detection-containers"
  format     = { docker = { standard = {} } }
  iam = {
    "roles/artifactregistry.writer" = [
      "serviceAccount:${data.google_project.project.number}@cloudbuild.gserviceaccount.com"
    ]
    "roles/artifactregistry.reader" = [
      module.dataflow_sa.iam_email,
      google_service_account.training.member
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

// Optional bucket for staging data and scripts
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

module "input_topic" {
  depends_on = [google_project_service.application]
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/pubsub?ref=v58.0.0"
  project_id = var.project_id
  name       = "anomaly-detection-transactions"
  subscriptions = {
    anomaly-detection-transactions-sub = {}
  }
}

module "output_topic" {
  depends_on = [google_project_service.application]
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/pubsub?ref=v58.0.0"
  project_id = var.project_id
  name       = "anomaly-detection-detections"
  subscriptions = {
    anomaly-detection-detections-sub = {}
  }
}

//bigtable table
module "enrichment_table" {
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
    customer_profiles = {
      column_families = { profile = {} }
    }
  }
}

//bigquery dataset
module "output_dataset" {
  depends_on = [google_project_service.application]
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/bigquery-dataset?ref=v58.0.0"
  project_id = var.project_id
  id         = local.bigquery_dataset
  location   = var.region
  options    = { delete_contents_on_destroy = var.destroy_all_resources }
}

// Service account
module "dataflow_sa" {
  depends_on = [google_project_service.application]
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v58.0.0"
  project_id = var.project_id
  name       = local.dataflow_service_account
  iam_project_roles = {
    (var.project_id) = [
      "roles/dataflow.worker",
      "roles/monitoring.metricWriter",
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

resource "local_file" "variables_script" {
  filename        = "${path.module}/../../pipelines/anomaly_detection/scripts/00_set_variables.sh"
  file_permission = "0644"
  content         = <<FILE
# This file is generated by the Terraform code of this Solution Guide.
# We recommend that you modify this file only through the Terraform deployment.
export PROJECT=${var.project_id}
export REGION=${var.region}
export SUBNETWORK=${local.subnetwork}
export NETWORK=$${SUBNETWORK}
export TEMP_LOCATION=gs://${local.bucket_name}/tmp
export SERVICE_ACCOUNT=${module.dataflow_sa.email}

export DOCKER_REPOSITORY=${module.registry_docker.name}
export IMAGE_NAME=dataflow-solutions-anomaly-detection
export DOCKER_TAG=0.1
export DOCKER_IMAGE=$REGION-docker.pkg.dev/$PROJECT/$DOCKER_REPOSITORY/$IMAGE_NAME

export CONTAINER_URI=$DOCKER_IMAGE:$DOCKER_TAG

export MAX_DATAFLOW_WORKERS=${local.max_dataflow_workers}
export DISK_SIZE_GB=${local.worker_disk_size_gb}
export MACHINE_TYPE=${local.machine_type}

export BIGTABLE_INSTANCE=${local.bigtable_instance}
export BIGTABLE_TABLE=customer_profiles
export BIGTABLE_COLUMN_FAMILY=profile
export BQ_DATASET=${module.output_dataset.dataset_id}
export BIGQUERY_TABLE=${var.project_id}.${module.output_dataset.dataset_id}.${google_bigquery_table.detections.table_id}
export BUCKET=${local.bucket_name}
export TRAINING_SERVICE_ACCOUNT=${google_service_account.training.email}
export TRAINING_CONTAINER_URI=$REGION-docker.pkg.dev/$PROJECT/$DOCKER_REPOSITORY/anomaly-training:$DOCKER_TAG
export SERVING_CONTAINER_URI=$REGION-docker.pkg.dev/$PROJECT/$DOCKER_REPOSITORY/anomaly-serving:$DOCKER_TAG
export ENDPOINT_PREDICTOR_ROLE=${google_project_iam_custom_role.predictor.id}
export INPUT_SUBSCRIPTION=projects/${var.project_id}/subscriptions/anomaly-detection-transactions-sub
export OUTPUT_TOPIC=projects/${var.project_id}/topics/anomaly-detection-detections
export ERROR_TOPIC=${google_pubsub_topic.errors.id}
export INPUT_TOPIC=projects/${var.project_id}/topics/anomaly-detection-transactions
export OUTPUT_SUBSCRIPTION=projects/${var.project_id}/subscriptions/anomaly-detection-detections-sub
export ERROR_SUBSCRIPTION=${google_pubsub_subscription.errors.id}
FILE
}
