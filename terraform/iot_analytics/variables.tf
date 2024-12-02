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

variable "billing_account" {
  description = "Billing account for the projects/resources"
  type        = string
  default     = null
}

variable "destroy_all_resources" {
  description = "Destroy all resources when calling tf destroy. Use false for production deployments. For test environments, set to true to remove all buckets and Spanner instances."
  type        = bool
  default     = true
}

variable "network_prefix" {
  description = "Prefix to be used for networks and subnetworks"
  type        = string
  default     = "dataflow"
}

variable "organization" {
  description = "Organization for the project/resources"
  type        = string
  default     = null
}

variable "project_create" {
  description = "True if you want to create a new project. False to reuse an existing project."
  type        = bool
}

variable "project_id" {
  description = "Project ID for the project/resources"
  type        = string
}

variable "region" {
  description = "The region for resources and networking"
  type        = string
}

variable "pubsub_topic" {
  description = "Name for your pub sub topic"
  type        = string
}