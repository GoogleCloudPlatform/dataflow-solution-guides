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

variable "project_id" {
  description = "Project ID of the existing GCP project where resources will be provisioned."
  type        = string
}

variable "region" {
  description = "The GCP region for application resources and Dataflow jobs."
  type        = string
}

variable "zone" {
  description = "Zone suffix (e.g. 'a') used for the Bigtable cluster and for GPU worker placement."
  type        = string
  default     = "a"
}

variable "subnetwork" {
  description = "Optional subnetwork URL or path for Dataflow workers (e.g. regions/europe-southwest1/subnetworks/dev-default or full URI). If omitted, the default network is used."
  type        = string
  default     = null
}

variable "bucket_name" {
  description = "Optional GCS bucket name for Dataflow temp/staging files. Defaults to project_id if not specified."
  type        = string
  default     = null
}

variable "create_bucket" {
  description = "Whether to create a new GCS bucket for temp/staging files. Set to false if using an existing bucket."
  type        = bool
  default     = false
}

variable "service_account_name" {
  description = "Name of the dedicated Dataflow worker service account to create."
  type        = string
  default     = "gaming-analytics-sa"
}

variable "machine_type" {
  description = "Dataflow worker machine type. Defaults to n2-standard-2 when left null. The model runs on the worker CPU through cross-language RunInference, so no GPU machine type is needed. Avoid the n1-standard types: they are not offered in newer regions such as europe-southwest1."
  type        = string
  default     = null
}

variable "input_topic" {
  description = "Name for the input Pub/Sub topic carrying raw gameplay events."
  type        = string
  default     = "gaming-events"
}

variable "output_topic" {
  description = "Name for the output Pub/Sub topic buffering in-game recommendations."
  type        = string
  default     = "gaming-recommendations"
}

variable "destroy_all_resources" {
  description = "Destroy all resources when calling tf destroy. Use false for production deployments. For test environments, set to true to remove all resources."
  type        = bool
  default     = true
}
