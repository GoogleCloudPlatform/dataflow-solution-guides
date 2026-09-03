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
package com.google.cloud.dataflow.solutions.clickstream_analytics;

import com.google.api.services.bigquery.model.TableRow;
import com.google.cloud.dataflow.solutions.clickstream_analytics.data.ClickstreamObjects.ClickstreamEvent;
import com.google.cloud.dataflow.solutions.clickstream_analytics.data.ClickstreamObjects.ParsingError;
import com.google.cloud.dataflow.solutions.clickstream_analytics.data.ClickstreamObjects.UserSession;
import com.google.cloud.dataflow.solutions.clickstream_analytics.data.SchemaUtils;
import com.google.cloud.dataflow.solutions.clickstream_analytics.extract.ClickstreamPubSubReader;
import com.google.cloud.dataflow.solutions.clickstream_analytics.load.ClickstreamBigQuerySinks;
import com.google.cloud.dataflow.solutions.clickstream_analytics.options.ClickstreamProcessingOptions;
import com.google.cloud.dataflow.solutions.clickstream_analytics.transform.BigTableEnrichment;
import com.google.cloud.dataflow.solutions.clickstream_analytics.transform.DeadletterConverter;
import com.google.cloud.dataflow.solutions.clickstream_analytics.transform.JsonToEvents;
import com.google.cloud.dataflow.solutions.clickstream_analytics.transform.SessionAnalytics;
import org.apache.beam.sdk.Pipeline;
import org.apache.beam.sdk.io.gcp.bigquery.WriteResult;
import org.apache.beam.sdk.options.PipelineOptionsFactory;
import org.apache.beam.sdk.transforms.Flatten;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PCollectionList;
import org.apache.beam.sdk.values.PCollectionTuple;

public class ClickstreamPubSubToBq {

    public static void main(String[] args) {
        PipelineOptionsFactory.register(ClickstreamProcessingOptions.class);
        ClickstreamProcessingOptions options =
                PipelineOptionsFactory.fromArgs(args)
                        .withValidation()
                        .as(ClickstreamProcessingOptions.class);

        Pipeline p = createPipeline(options);
        p.run();
    }

    public static Pipeline createPipeline(ClickstreamProcessingOptions options) {
        Pipeline p = Pipeline.create(options);

        final String bqProject = options.getBqProjectId();
        final String bqDataset = options.getBqDataset();
        final String bqTable = options.getBqTable();
        final String bqSessionsTable = options.getBqSessionsTable();
        final String bqDeadletterTable = options.getOutputDeadletterTable();
        final String subscription = options.getSubscription();
        final String btInstance = options.getBtInstance();
        final String btTable = options.getBtTable();
        final String btLookupKey = options.getBtLookupKey();
        final int sessionGapMinutes =
                options.getSessionGapDurationMinutes() != null
                        ? options.getSessionGapDurationMinutes()
                        : 30;
        final boolean enableBigtable =
                options.getEnableBigtableEnrichment() != null
                        ? options.getEnableBigtableEnrichment()
                        : true;

        // E: Extract raw JSON messages from Pub/Sub
        PCollection<String> pubsubMessages =
                p.apply("ReadPubSubData", ClickstreamPubSubReader.fromSubscription(subscription));

        // T: Transform JSON strings into strongly typed ClickstreamEvent objects
        PCollectionTuple parseResults =
                pubsubMessages.apply("TransformJSONToEvents", JsonToEvents.create());

        PCollection<ClickstreamEvent> validEvents = parseResults.get(JsonToEvents.SUCCESS_TAG);
        PCollection<ParsingError> parseErrors = parseResults.get(JsonToEvents.ERROR_TAG);

        PCollection<TableRow> parseErrorRows =
                parseErrors.apply("ParseErrorsToDeadletter", DeadletterConverter.fromParseErrors());

        // T: Enrich events with Cloud Bigtable metadata
        PCollection<ClickstreamEvent> enrichedEvents =
                validEvents.apply(
                        "EnrichWithBigtable",
                        BigTableEnrichment.create()
                                .withProjectId(bqProject)
                                .withInstanceId(btInstance)
                                .withTableId(btTable)
                                .withLookupKeyField(btLookupKey)
                                .withEnabled(enableBigtable));

        // L: Stream 1 - Write Enriched Raw Events to BigQuery
        WriteResult eventsWriteResult =
                enrichedEvents.apply(
                        "WriteEventsToBQ",
                        ClickstreamBigQuerySinks.writeEvents()
                                .withProjectId(bqProject)
                                .withDataset(bqDataset)
                                .withTable(bqTable)
                                .build());

        // T: Stream 2 - Compute Session Windowing Analytics
        PCollection<UserSession> sessionSummaries =
                enrichedEvents.apply(
                        "ComputeSessionAnalytics", SessionAnalytics.of(sessionGapMinutes));

        // L: Stream 2 - Write Aggregated Sessions to BigQuery with Storage API UPSERTs
        WriteResult sessionsWriteResult =
                sessionSummaries.apply(
                        "WriteSessionsToBQ",
                        ClickstreamBigQuerySinks.writeSessions()
                                .withProjectId(bqProject)
                                .withDataset(bqDataset)
                                .withTable(bqSessionsTable)
                                .build());

        // T: Capture BigQuery Storage Write API insert failures from both streams
        PCollection<TableRow> eventsInsertErrors =
                eventsWriteResult
                        .getFailedStorageApiInserts()
                        .apply(
                                "EventsInsertErrorsToDeadletter",
                                DeadletterConverter.fromStorageApiErrors());

        PCollection<TableRow> sessionsInsertErrors =
                sessionsWriteResult
                        .getFailedStorageApiInserts()
                        .apply(
                                "SessionsInsertErrorsToDeadletter",
                                DeadletterConverter.fromStorageApiErrors());

        // L: Combine and write all error records to BigQuery dead-letter table
        PCollectionList<TableRow> allDeadletters =
                PCollectionList.of(parseErrorRows)
                        .and(eventsInsertErrors)
                        .and(sessionsInsertErrors);

        allDeadletters
                .apply("FlattenDeadletterRows", Flatten.pCollections())
                .apply(
                        "WriteDeadletterToBigQuery",
                        ClickstreamBigQuerySinks.writeDeadletter()
                                .withProjectId(bqProject)
                                .withDataset(bqDataset)
                                .withTable(bqDeadletterTable)
                                .withJsonSchema(SchemaUtils.getDeadletterTableSchemaJson())
                                .build());

        return p;
    }
}
