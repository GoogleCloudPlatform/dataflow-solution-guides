/*
 * Copyright 2026 Google.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package com.google.cloud.dataflow.solutions.gaming_analytics.options;

import org.apache.beam.sdk.options.Default;
import org.apache.beam.sdk.options.Description;
import org.apache.beam.sdk.options.PipelineOptions;
import org.apache.beam.sdk.options.Validation;

/**
 * Pipeline options for the gaming analytics guide.
 *
 * <p>The defaults deliberately match the resource names created by {@code
 * terraform/gaming_analytics}, and the launch script maps every option to a variable exported by
 * the generated {@code scripts/00_set_environment.sh}.
 */
public interface GamingAnalyticsOptions extends PipelineOptions {

    @Validation.Required
    @Description("Pub/Sub subscription with the raw gameplay events (INPUT_SUBSCRIPTION)")
    String getInputSubscription();

    void setInputSubscription(String value);

    @Validation.Required
    @Description("Pub/Sub topic where in-game recommendations are published (OUTPUT_TOPIC)")
    String getOutputTopic();

    void setOutputTopic(String value);

    @Validation.Required
    @Description("Pub/Sub dead-letter topic for unprocessable elements (ERROR_TOPIC)")
    String getErrorTopic();

    void setErrorTopic(String value);

    @Description("Project owning the Bigtable feature store. Defaults to the Dataflow project.")
    String getBigtableProject();

    void setBigtableProject(String value);

    @Validation.Required
    @Description("Cloud Bigtable instance holding the player feature store (BIGTABLE_INSTANCE)")
    String getBigtableInstance();

    void setBigtableInstance(String value);

    @Validation.Required
    @Description("Cloud Bigtable table with the player features (BIGTABLE_TABLE)")
    String getBigtableTable();

    void setBigtableTable(String value);

    @Description("Cloud Bigtable column family with the player features (BIGTABLE_COLUMN_FAMILY)")
    @Default.String("features")
    String getBigtableColumnFamily();

    void setBigtableColumnFamily(String value);

    @Description("Set to false to skip the Bigtable lookups, for example for a smoke test")
    @Default.Boolean(true)
    Boolean getEnableEnrichment();

    void setEnableEnrichment(Boolean value);

    @Validation.Required
    @Description("Destination BigQuery table, as PROJECT:DATASET.TABLE or PROJECT.DATASET.TABLE")
    String getBigQueryTable();

    void setBigQueryTable(String value);

    @Validation.Required
    @Description(
            "Cloud Storage URI of the pickled scikit-learn model loaded by RunInference"
                    + " (MODEL_URI), as gs://BUCKET/OBJECT. Produce it with"
                    + " scripts/train_model.py.")
    String getModelUri();

    void setModelUri(String value);

    @Description(
            "Optional address, as host:port, of an already running Python expansion service"
                    + " (EXPANSION_SERVICE). When it is empty, Beam starts a transient one at"
                    + " submission time, installs the pinned scikit-learn, NumPy and pandas"
                    + " versions into its virtualenv, and stages those same versions for the"
                    + " workers.")
    String getExpansionService();

    void setExpansionService(String value);
}
