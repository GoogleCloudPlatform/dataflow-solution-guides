#  Copyright 2026 The synthetic-llm-dataflow-bigquery Authors
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
  public_dataset = "bigquery-public-data.thelook_ecommerce"
  source_fqn     = "${var.project_id}.${local.source_dataset}"
  landing_fqn    = "${var.project_id}.${local.landing_dataset}"

  # One script, run once: the source snapshots the launch reads, filtered so
  # every child row has its parent (referential integrity in the source),
  # and the products catalog in the landing dataset as an already-landed
  # parent. GEOGRAPHY columns are left out: the generator treats them as
  # free text and BigQuery would reject the invented WKT on load.
  snapshot_sql = <<-SQL
    CREATE OR REPLACE TABLE `${local.landing_fqn}.products` AS
    SELECT * FROM `${local.public_dataset}.products`;

    CREATE OR REPLACE TABLE `${local.source_fqn}.users` AS
    SELECT * EXCEPT (user_geom) FROM `${local.public_dataset}.users`;

    CREATE OR REPLACE TABLE `${local.source_fqn}.orders` AS
    SELECT o.* FROM `${local.public_dataset}.orders` AS o
    WHERE o.user_id IN (SELECT id FROM `${local.source_fqn}.users`);

    CREATE OR REPLACE TABLE `${local.source_fqn}.order_items` AS
    SELECT i.* FROM `${local.public_dataset}.order_items` AS i
    WHERE EXISTS (
      SELECT 1 FROM `${local.source_fqn}.orders` AS o
      WHERE o.order_id = i.order_id AND o.user_id = i.user_id)
    AND i.product_id IN (SELECT id FROM `${local.landing_fqn}.products`);
  SQL

  quality_tables = {
    dlq             = "dlq_inserted_at"
    validation_runs = "created_at"
    fk_fanout_stats = null
  }
}

module "source_dataset" {
  depends_on = [google_project_service.application]
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/bigquery-dataset?ref=v58.0.0"
  project_id = var.project_id
  id         = local.source_dataset
  location   = var.bq_location
  options    = { delete_contents_on_destroy = var.destroy_all_resources }
}

module "landing_dataset" {
  depends_on = [google_project_service.application]
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/bigquery-dataset?ref=v58.0.0"
  project_id = var.project_id
  id         = local.landing_dataset
  location   = var.bq_location
  options    = { delete_contents_on_destroy = var.destroy_all_resources }
}

module "quality_dataset" {
  depends_on = [google_project_service.application]
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/bigquery-dataset?ref=v58.0.0"
  project_id = var.project_id
  id         = local.quality_dataset
  location   = var.bq_location
  options    = { delete_contents_on_destroy = var.destroy_all_resources }
}

// Pipeline-written quality tables; schemas are the pipeline's own contracts.
resource "google_bigquery_table" "quality" {
  for_each            = local.quality_tables
  project             = var.project_id
  dataset_id          = module.quality_dataset.dataset_id
  table_id            = each.key
  schema              = file("${local.pipeline_dir}/config/bq_schema/synthetic_data_quality/${each.key}.schema.json")
  deletion_protection = !var.destroy_all_resources

  dynamic "time_partitioning" {
    for_each = each.value == null ? [] : [each.value]
    content {
      type  = "DAY"
      field = time_partitioning.value
    }
  }
}

resource "random_id" "snapshot" {
  byte_length = 4
  keepers = {
    sql = sha256(local.snapshot_sql)
  }
}

resource "google_bigquery_job" "thelook_snapshot" {
  project  = var.project_id
  location = var.bq_location
  job_id   = "sdfb-thelook-snapshot-${random_id.snapshot.hex}"

  query {
    query              = local.snapshot_sql
    use_legacy_sql     = false
    create_disposition = ""
    write_disposition  = ""
  }

  depends_on = [module.source_dataset, module.landing_dataset]
}
