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

import static org.apache.beam.sdk.io.gcp.bigquery.BigQueryIO.Write.CreateDisposition.CREATE_IF_NEEDED;
import static org.apache.beam.sdk.io.gcp.bigquery.BigQueryIO.Write.CreateDisposition.CREATE_NEVER;
import static org.apache.beam.sdk.io.gcp.bigquery.BigQueryIO.Write.WriteDisposition.WRITE_APPEND;

import com.google.api.services.bigquery.model.TableRow;
import com.google.cloud.dataflow.solutions.clickstream_analytics.data.ClickstreamObjects.ClickstreamEvent;
import com.google.cloud.dataflow.solutions.clickstream_analytics.data.ClickstreamObjects.UserSession;
import com.google.common.io.Resources;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import org.apache.beam.sdk.Pipeline;
import org.apache.beam.sdk.io.gcp.bigquery.BigQueryIO;
import org.apache.beam.sdk.io.gcp.bigquery.RowMutationInformation;
import org.apache.beam.sdk.io.gcp.bigquery.WriteResult;
import org.apache.beam.sdk.io.gcp.pubsub.PubsubIO;
import org.apache.beam.sdk.options.Default;
import org.apache.beam.sdk.options.Description;
import org.apache.beam.sdk.options.PipelineOptions;
import org.apache.beam.sdk.options.PipelineOptionsFactory;
import org.apache.beam.sdk.transforms.Flatten;
import org.apache.beam.sdk.transforms.MapElements;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.transforms.SimpleFunction;
import org.apache.beam.sdk.values.KV;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PCollectionList;
import org.apache.beam.sdk.values.PCollectionTuple;

public class ClickstreamPubSubToBq {

    private static final String DEADLETTER_SCHEMA_FILE_PATH =
            "streaming_source_deadletter_table_schema.json";

    public interface MyOptions extends PipelineOptions {
        @Description("BigQuery Project ID")
        String getBqProjectId();

        void setBqProjectId(String value);

        @Description("BigQuery Dataset Name")
        String getBQDataset();

        void setBQDataset(String value);

        @Description("BigQuery Table for Enriched Events")
        String getBQTable();

        void setBQTable(String value);

        @Description("BigQuery Table for Aggregated Sessions")
        @Default.String("sessions")
        String getBQSessionsTable();

        void setBQSessionsTable(String value);

        @Description("PubSub Subscription Name")
        String getSubscription();

        void setSubscription(String value);

        @Description("BigTable Instance Name")
        String getBTInstance();

        void setBTInstance(String value);

        @Description("BigTable Table Name")
        String getBTTable();

        void setBTTable(String value);

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

    public static void main(String[] args) {
        PipelineOptionsFactory.register(MyOptions.class);
        MyOptions options =
                PipelineOptionsFactory.fromArgs(args).withValidation().as(MyOptions.class);

        Pipeline p = Pipeline.create(options);

        final String PROJECT = options.getBqProjectId();
        final String SUBSCRIPTION = options.getSubscription();
        final String BQ_PROJECT = PROJECT;
        final String BQ_DATASET = options.getBQDataset();
        final String BQ_TABLE = options.getBQTable();
        final String BQ_SESSIONS_TABLE = options.getBQSessionsTable();
        final String BQ_DEADLETTER_TABLE = options.getOutputDeadletterTable();
        final String BT_INSTANCE = options.getBTInstance();
        final String BT_TABLE = options.getBTTable();
        final String BT_LOOKUP_KEY = options.getBtLookupKey();
        final int SESSION_GAP_MINUTES =
                options.getSessionGapDurationMinutes() != null
                        ? options.getSessionGapDurationMinutes()
                        : 30;
        final boolean ENABLE_BIGTABLE =
                options.getEnableBigtableEnrichment() != null
                        ? options.getEnableBigtableEnrichment()
                        : true;

        // 1. Read raw JSON messages from Pub/Sub
        PCollection<String> pubsubMessages =
                p.apply("ReadPubSubData", PubsubIO.readStrings().fromSubscription(SUBSCRIPTION));

        // 2. Parse JSON strings into strongly typed ClickstreamEvent objects
        PCollectionTuple parseResults =
                pubsubMessages.apply("TransformJSONToEvents", JsonToEvents.run());

        PCollection<ClickstreamEvent> validEvents = parseResults.get(JsonToEvents.SUCCESS_TAG);
        PCollection<KV<String, String>> parseErrors = parseResults.get(JsonToEvents.FAILURE_TAG);

        PCollection<TableRow> parseErrorRows =
                parseErrors.apply("ParseErrorsToDeadletter", DeadletterConverter.fromParseErrors());

        // 3. Enrich events with Cloud Bigtable metadata
        PCollection<ClickstreamEvent> enrichedEvents =
                validEvents.apply(
                        "EnrichWithBigtable",
                        ParDo.of(
                                new BigTableEnrichment(
                                        PROJECT,
                                        BT_INSTANCE,
                                        BT_TABLE,
                                        BT_LOOKUP_KEY,
                                        ENABLE_BIGTABLE)));

        // 4. Stream 1: Write Enriched Raw Events to BigQuery
        PCollection<TableRow> enrichedEventRows =
                enrichedEvents.apply(
                        "EventsToTableRow",
                        MapElements.via(
                                new SimpleFunction<ClickstreamEvent, TableRow>() {
                                    @Override
                                    public TableRow apply(ClickstreamEvent event) {
                                        return event.toTableRow();
                                    }
                                }));

        WriteResult eventsWriteResult =
                enrichedEventRows.apply(
                        "WriteEventsToBQ",
                        BigQueryIO.writeTableRows()
                                .withMethod(BigQueryIO.Write.Method.STORAGE_API_AT_LEAST_ONCE)
                                .withWriteDisposition(WRITE_APPEND)
                                .withCreateDisposition(CREATE_NEVER)
                                .ignoreUnknownValues()
                                .to(String.format("%s:%s.%s", BQ_PROJECT, BQ_DATASET, BQ_TABLE)));

        // 5. Stream 2: Compute Session Windowing Analytics & Write with Storage API UPSERTs
        PCollection<UserSession> sessionSummaries =
                enrichedEvents.apply(
                        "ComputeSessionAnalytics", SessionAnalytics.of(SESSION_GAP_MINUTES));

        PCollection<TableRow> sessionRows =
                sessionSummaries.apply(
                        "SessionsToTableRow",
                        MapElements.via(
                                new SimpleFunction<UserSession, TableRow>() {
                                    @Override
                                    public TableRow apply(UserSession session) {
                                        return session.toTableRow();
                                    }
                                }));

        WriteResult sessionsWriteResult =
                sessionRows.apply(
                        "WriteSessionsToBQ",
                        BigQueryIO.writeTableRows()
                                .withMethod(BigQueryIO.Write.Method.STORAGE_API_AT_LEAST_ONCE)
                                .withWriteDisposition(WRITE_APPEND)
                                .withCreateDisposition(CREATE_NEVER)
                                .withPrimaryKey(Collections.singletonList("session_id"))
                                .withRowMutationInformationFn(
                                        row ->
                                                RowMutationInformation.of(
                                                        RowMutationInformation.MutationType.UPSERT,
                                                        String.valueOf(
                                                                ((Number)
                                                                                row.getOrDefault(
                                                                                        "event_count",
                                                                                        1))
                                                                        .longValue())))
                                .to(
                                        String.format(
                                                "%s:%s.%s",
                                                BQ_PROJECT, BQ_DATASET, BQ_SESSIONS_TABLE)));

        // 6. Capture BigQuery Storage Write API insert failures from both streams
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

        // 7. Write all combined error records to BigQuery dead-letter table
        PCollectionList<TableRow> allDeadletters =
                PCollectionList.of(parseErrorRows)
                        .and(eventsInsertErrors)
                        .and(sessionsInsertErrors);

        allDeadletters
                .apply("FlattenDeadletterRows", Flatten.pCollections())
                .apply(
                        "WriteDeadletterToBigQuery",
                        BigQueryIO.writeTableRows()
                                .withMethod(BigQueryIO.Write.Method.STORAGE_API_AT_LEAST_ONCE)
                                .withWriteDisposition(WRITE_APPEND)
                                .withCreateDisposition(CREATE_IF_NEEDED)
                                .withJsonSchema(getDeadletterTableSchemaJson())
                                .to(
                                        String.format(
                                                "%s:%s.%s",
                                                BQ_PROJECT, BQ_DATASET, BQ_DEADLETTER_TABLE)));

        p.run();
    }

    static String getDeadletterTableSchemaJson() {
        String schemaJson = null;
        try {
            schemaJson =
                    Resources.toString(
                            Resources.getResource(DEADLETTER_SCHEMA_FILE_PATH),
                            StandardCharsets.UTF_8);
        } catch (Exception e) {
            System.err.println(
                    "Unable to read "
                            + DEADLETTER_SCHEMA_FILE_PATH
                            + " file from resources folder!");
        }
        return schemaJson;
    }
}
