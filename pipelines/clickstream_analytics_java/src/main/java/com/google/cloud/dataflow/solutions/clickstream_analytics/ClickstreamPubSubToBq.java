/*
 * Copyright 2024 Google.
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

import static com.google.cloud.dataflow.solutions.clickstream_analytics.JsonToTableRows.SUCCESS_TAG;
import static org.apache.beam.sdk.io.gcp.bigquery.BigQueryIO.Write.CreateDisposition.CREATE_NEVER;
import static org.apache.beam.sdk.io.gcp.bigquery.BigQueryIO.Write.WriteDisposition.WRITE_APPEND;

import com.google.api.services.bigquery.model.TableRow;
import com.google.common.io.Resources;
import java.nio.charset.StandardCharsets;
import org.apache.beam.sdk.Pipeline;
import org.apache.beam.sdk.io.gcp.bigquery.*;
import org.apache.beam.sdk.io.gcp.pubsub.PubsubIO;
import org.apache.beam.sdk.options.Description;
import org.apache.beam.sdk.options.PipelineOptions;
import org.apache.beam.sdk.options.PipelineOptionsFactory;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.DoFn.Element;
import org.apache.beam.sdk.transforms.DoFn.ProcessElement;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PCollectionTuple;

public class ClickstreamPubSubToBq {

    private static final String DEADLETTER_SCHEMA_FILE_PATH =
            "streaming_source_deadletter_table_schema.json";

    public interface MyOptions extends PipelineOptions {
        // BQ Project Name
        @Description("Project ID")
        String getBqProjectId();

        void setBqProjectId(String value);

        // BQ Dataset Name
        @Description("BigQuery dataset")
        String getBQDataset();

        void setBQDataset(String value);

        // BQ Table Name
        @Description("BigQuery Table")
        String getBQTable();

        void setBQTable(String value);

        // PubSub Subscription Name
        @Description("Pubsub subscription")
        String getSubscription();

        void setSubscription(String value);

        // BigTable Instance Name
        @Description("BigTable Instance")
        String getBTInstance();

        void setBTInstance(String value);

        // BigTable Table Name
        @Description("BigTable table")
        String getBTTable();

        void setBTTable(String value);

        // BQ Deadletter Table
        @Description("BQ Deadletter table")
        String getOutputDeadletterTable();

        void setOutputDeadletterTable(String value);

        // BT Lookup Key
        @Description("BT Lookup Key")
        String getBtLookupKey();

        void setBtLookupKey(String value);
    }

    public static void main(String[] args) {

        PipelineOptionsFactory.register(MyOptions.class);
        MyOptions options =
                PipelineOptionsFactory.fromArgs(args).withValidation().as(MyOptions.class);

        Pipeline p = Pipeline.create(options);

        final String PROJECT = options.getBqProjectId();
        final String SUBSCRIPTION = options.getSubscription();
        final String BQ_PROJECT = PROJECT;
        final String BQ_TABLE = options.getBQTable();
        final String BQ_DEADLETTER_TABLE = options.getOutputDeadletterTable();
        final String BQ_DATASET = options.getBQDataset();
        final String BT_INSTANCE = options.getBTInstance();
        final String BT_TABLE = options.getBTTable();
        final String BT_LOOKUP_KEY = options.getBtLookupKey();

        PCollection<String> pubsubMessages =
                p.apply("ReadPubSubData", PubsubIO.readStrings().fromSubscription(SUBSCRIPTION));

        //  TODO: Bigtable enrichment with pubsub data task is still pending.
        //  PCollection<String> enrichedMessages = pubsubMessages.apply(
        //  "EnrichWithBigtable",
        //  ParDo.of(new BigTableEnrichment(PROJECT, BT_INSTANCE, BT_TABLE,BT_LOOKUP_KEY))
        //  );

        // Convert PubSub data to BigQuery Table Rows
        PCollectionTuple results = pubsubMessages.apply("TransformJSONToBQ", JsonToTableRows.run());

        // TODO: Apply analytics to windowwed data
        // PCollection<TableRow> windowedTableRows =
        //     results
        //         .get(SUCCESS_TAG)
        //         .apply(
        //             "SessionWindow",
        //
        // Window.<TableRow>into(Sessions.withGapDuration(Duration.standardMinutes(1))));

        // Write Data to BigQuery Table
        WriteResult writeResult =
                results.get(SUCCESS_TAG)
                        .apply(
                                "WriteSuccessfulRecordsToBQ",
                                BigQueryIO.writeTableRows()
                                        .withMethod(
                                                BigQueryIO.Write.Method.STORAGE_API_AT_LEAST_ONCE)
                                        .withWriteDisposition(WRITE_APPEND)
                                        .withCreateDisposition(CREATE_NEVER)
                                        .ignoreUnknownValues()
                                        .skipInvalidRows()
                                        .to(
                                                (row) -> {
                                                    return new TableDestination(
                                                            String.format(
                                                                    "%s:%s.%s",
                                                                    BQ_PROJECT,
                                                                    BQ_DATASET,
                                                                    BQ_TABLE),
                                                            "BQ Table destination");
                                                }));

        //  Failed insertion will be pushed to Bigquery Deadletter table  *********/
        writeResult
                .getFailedStorageApiInserts()
                .apply(
                        "FailedRecordToTableRow",
                        ParDo.of(
                                new DoFn<BigQueryStorageApiInsertError, TableRow>() {
                                    @ProcessElement
                                    public void process(
                                            @Element BigQueryStorageApiInsertError insertError,
                                            OutputReceiver<TableRow> outputReceiver) {
                                        outputReceiver.output(insertError.getRow());
                                    }
                                }))
                .apply(
                        "WriteFailedRecordsToBigQuery",
                        BigQueryIO.writeTableRows()
                                .withMethod(BigQueryIO.Write.Method.STORAGE_API_AT_LEAST_ONCE)
                                .to(
                                        (row) -> {
                                            return new TableDestination(
                                                    String.format(
                                                            "%s:%s.%s",
                                                            BQ_PROJECT,
                                                            BQ_DATASET,
                                                            BQ_DEADLETTER_TABLE),
                                                    "BQ Dead Letter Table destination");
                                        })
                                .withJsonSchema(getDeadletterTableSchemaJson())
                                .withCreateDisposition(
                                        BigQueryIO.Write.CreateDisposition.CREATE_IF_NEEDED)
                                .withWriteDisposition(
                                        BigQueryIO.Write.WriteDisposition.WRITE_APPEND));
        p.run();
    }

    // Read BigQuery Deadletter JSON Schema if table not exist
    static String getDeadletterTableSchemaJson() {
        String schemaJson = null;
        try {
            schemaJson =
                    Resources.toString(
                            Resources.getResource(DEADLETTER_SCHEMA_FILE_PATH),
                            StandardCharsets.UTF_8);
        } catch (Exception e) {
            System.out.println("Unable to read {} file from the resources folder!");
        }

        return schemaJson;
    }
}
