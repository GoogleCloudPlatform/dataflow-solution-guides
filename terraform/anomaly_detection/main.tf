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
  max_dataflow_workers     = 1
  worker_disk_size_gb      = 200
  machine_type             = "g2-standard-4"
  bigquery_dataset         = "output_dataset"
  bigtable_instance        = "bt-enrichment"
}


// Project
module "google_cloud_project" {
  source          = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/project?ref=v36.0.1"
  billing_account = var.billing_account
  project_create  = var.project_create
  name            = var.project_id
  parent          = var.organization
  services = [
    "cloudbuild.googleapis.com",
    "dataflow.googleapis.com",
    "monitoring.googleapis.com",
    "pubsub.googleapis.com",
    "autoscaling.googleapis.com",
    "artifactregistry.googleapis.com",
    "bigtable.googleapis.com",
    "bigtableadmin.googleapis.com",
    "bigquery.googleapis.com",
  ]
}

module "registry_docker" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/artifact-registry?ref=v36.0.1"
  project_id = module.google_cloud_project.project_id
  location   = var.region
  name       = "dataflow-containers"
  format     = { docker = { standard = {} } }
  iam = {
    "roles/artifactregistry.admin" = [
      "serviceAccount:${module.google_cloud_project.number}@cloudbuild.gserviceaccount.com"
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
  name       = "transactions"
  subscriptions = {
    transactions-sub = {}
  }
}

module "output_topic" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/pubsub?ref=v36.0.1"
  project_id = module.google_cloud_project.project_id
  name       = "detections"
  subscriptions = {
    detections-sub = {}
  }
}

//bigtable table
module "enrichment_table" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/bigtable-instance?ref=v36.0.1"
  project_id = module.google_cloud_project.project_id
  name       = local.bigtable_instance
  clusters = {
    cluster1 = {
      zone      = "${var.region}-${var.zone}"
      num_nodes = 3
    }
  }
  tables = {
    features = {}
  }
}

//bigquery dataset
module "output_dataset" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/bigquery-dataset?ref=v36.0.1"
  project_id = module.google_cloud_project.project_id
  id         = local.bigquery_dataset
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
module "regional_nat" {
  // So we can get to Internet if necessary (from the Dataflow region)
  source         = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/net-cloudnat?ref=v36.0.1"
  project_id     = module.google_cloud_project.project_id
  region         = var.region
  name           = "${var.network_prefix}-nat"
  router_network = module.vpc_network.self_link
}

resource "local_file" "variables_script" {
  filename        = "${path.module}/../../pipelines/anomaly_detection/scripts/00_set_variables.sh"
  file_permission = "0644"
  content         = <<FILE
# This file is generated by the Terraform code of this Solution Guide.
# We recommend that you modify this file only through the Terraform deployment.
export PROJECT=${module.google_cloud_project.project_id}
export REGION=${var.region}
export SUBNETWORK=regions/${var.region}/subnetworks/${var.network_prefix}-subnet
export TEMP_LOCATION=gs://$PROJECT/tmp
export SERVICE_ACCOUNT=${module.dataflow_sa.email}

export DOCKER_REPOSITORY=${module.registry_docker.name}
export IMAGE_NAME=dataflow-solutions-anomaly-detection
export DOCKER_TAG=0.1
export DOCKER_IMAGE=$REGION-docker.pkg.dev/$PROJECT/$DOCKER_REPOSITORY/$IMAGE_NAME

export CONTAINER_URI=$DOCKER_IMAGE:$DOCKER_TAG

export MAX_DATAFLOW_WORKERS=${local.max_dataflow_workers}
export DISK_SIZE_GB=${local.worker_disk_size_gb}
export MACHINE_TYPE=${local.machine_type}

export BIGTABLE_INSTANCE=${module.enrichment_table.id}
export BQ_DATASET=${module.output_dataset.dataset_id}
FILE
}
