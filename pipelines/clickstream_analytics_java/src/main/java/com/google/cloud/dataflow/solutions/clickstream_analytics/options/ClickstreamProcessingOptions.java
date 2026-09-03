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
package com.google.cloud.dataflow.solutions.clickstream_analytics.options;

import org.apache.beam.sdk.options.Default;
import org.apache.beam.sdk.options.Description;
import org.apache.beam.sdk.options.PipelineOptions;
import org.apache.beam.sdk.options.Validation;

public interface ClickstreamProcessingOptions extends PipelineOptions {

    @Validation.Required
    @Description("BigQuery Project ID")
    String getBqProjectId();

    void setBqProjectId(String value);

    @Validation.Required
    @Description("BigQuery Dataset Name")
    String getBqDataset();

    void setBqDataset(String value);

    @Validation.Required
    @Description("BigQuery Table for Enriched Events")
    String getBqTable();

    void setBqTable(String value);

    @Description("BigQuery Table for Aggregated Sessions")
    @Default.String("sessions")
    String getBqSessionsTable();

    void setBqSessionsTable(String value);

    @Validation.Required
    @Description("PubSub Subscription Name")
    String getSubscription();

    void setSubscription(String value);

    @Validation.Required
    @Description("BigTable Instance Name")
    String getBtInstance();

    void setBtInstance(String value);

    @Validation.Required
    @Description("BigTable Table Name")
    String getBtTable();

    void setBtTable(String value);

    @Validation.Required
    @Description("BigQuery Deadletter Table Name")
    String getOutputDeadletterTable();

    void setOutputDeadletterTable(String value);

    @Description("BigTable Lookup Key Field (e.g. curr, prev, user_id)")
    @Default.String("curr")
    String getBtLookupKey();

    void setBtLookupKey(String value);

    @Description("Session Inactivity Gap Duration in Minutes")
    @Default.Integer(30)
    Integer getSessionGapDurationMinutes();

    void setSessionGapDurationMinutes(Integer value);

    @Description("Enable BigTable Enrichment")
    @Default.Boolean(true)
    Boolean getEnableBigtableEnrichment();

    void setEnableBigtableEnrichment(Boolean value);
}
