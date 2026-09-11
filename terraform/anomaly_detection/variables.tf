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

variable "destroy_all_resources" {
  description = "Destroy all resources when calling tf destroy. Use false for production deployments. For test environments, set to true to remove all buckets and bigtable instances."
  type        = bool
  default     = true
}

variable "project_id" {
  description = "Existing project ID for application resources"
  type        = string
}

variable "region" {
  description = "Region for application resources and Dataflow"
  type        = string
}

variable "zone" {
  description = "The zone for big table. Just a single letter specifying a zone in the region. The default is zone a"
  type        = string
  default     = "a"
}

variable "subnetwork" {
  description = "Optional local subnet path or full Shared VPC URL. Omit to use the default network."
  type        = string
  default     = null
}

variable "bucket_name" {
  description = "Existing or new bucket name; defaults to project_id."
  type        = string
  default     = null
}

variable "create_bucket" {
  description = "Create and manage the bucket instead of reusing an existing bucket."
  type        = bool
  default     = false
}

variable "service_account_name" {
  description = "Dedicated Dataflow worker service account ID."
  type        = string
  default     = "anomaly-detection-sa"
  nullable    = false
}

variable "training_service_account_name" {
  description = "Dedicated Vertex AI custom training service account ID."
  type        = string
  default     = "anomaly-training-sa"
  nullable    = false
}

// N2 rather than N1 throughout: the n1-standard types are not offered in the
// newer regions (europe-southwest1 lists no n1-standard-* at all), so an N1
// default makes this guide fail on first deploy there. Dataflow rejects the
// job with "Unable to get machine type information for machine type
// n1-standard-2". n2-standard-2 and n2-standard-4 are available broadly and
// are supported by Vertex AI custom training and prediction alike.
variable "machine_type" {
  description = "Dataflow worker machine type. Avoid the n1-standard types: they are not offered in newer regions such as europe-southwest1."
  type        = string
  default     = "n2-standard-2"
}

variable "training_machine_type" {
  description = "Vertex AI training machine type. Avoid the n1-standard types: they are not offered in newer regions such as europe-southwest1."
  type        = string
  default     = "n2-standard-4"
}

variable "endpoint_machine_type" {
  description = "Vertex AI prediction endpoint machine type. Avoid the n1-standard types: they are not offered in newer regions such as europe-southwest1."
  type        = string
  default     = "n2-standard-2"
}
