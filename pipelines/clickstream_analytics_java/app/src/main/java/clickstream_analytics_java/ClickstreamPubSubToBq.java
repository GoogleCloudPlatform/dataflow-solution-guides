package clickstream_analytics_java;

import com.google.api.client.json.JsonFactory;
import com.google.api.services.bigquery.model.TableRow;
import com.google.common.io.Resources;
import org.apache.beam.runners.dataflow.DataflowRunner;
import org.apache.beam.runners.dataflow.options.DataflowPipelineOptions;
import org.apache.beam.runners.direct.DirectOptions;
import org.apache.beam.sdk.Pipeline;
import org.apache.beam.sdk.coders.StringUtf8Coder;
import org.apache.beam.sdk.extensions.gcp.util.Transport;
import org.apache.beam.sdk.io.gcp.bigquery.*;
import org.apache.beam.sdk.io.gcp.pubsub.PubsubIO;
import org.apache.beam.sdk.io.gcp.pubsub.PubsubMessage;
import org.apache.beam.sdk.io.gcp.pubsub.PubsubMessageWithAttributesCoder;
import org.apache.beam.sdk.options.Default;
import org.apache.beam.sdk.options.Description;
import org.apache.beam.sdk.options.PipelineOptionsFactory;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.MapElements;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.transforms.windowing.Sessions;
import org.apache.beam.sdk.transforms.windowing.Window;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PCollectionTuple;
import org.joda.time.Duration;

import java.io.IOException;
import java.nio.charset.StandardCharsets;

import static clickstream_analytics_java.JsonToTableRows.SUCCESS_TAG;
import static org.apache.beam.sdk.io.gcp.bigquery.BigQueryIO.Write.CreateDisposition.CREATE_NEVER;
import static org.apache.beam.sdk.io.gcp.bigquery.BigQueryIO.Write.WriteDisposition.WRITE_APPEND;

public class ClickstreamPubSubToBq {

    public static final FailsafeElementCoder<PubsubMessage, String> CODER =
            FailsafeElementCoder.of(PubsubMessageWithAttributesCoder.of(), StringUtf8Coder.of());

    public static final FailsafeElementCoder<String, String> FAILSAFE_ELEMENT_CODER =
            FailsafeElementCoder.of(StringUtf8Coder.of(), StringUtf8Coder.of());

    private static final JsonFactory JSON_FACTORY = Transport.getJsonFactory();

    private static final String DEADLETTER_SCHEMA_FILE_PATH =
            "streaming_source_deadletter_table_schema.json";

    public interface MyOptions extends DataflowPipelineOptions, DirectOptions {
//     *******   BQ Project Name   *******

        @Description("Project ID")
        @Default.String("project_id")
        String getProjectId();

        void setProjectId(String value);

//    *******    BQ Dataset Name   *******

        @Description("BigQuery dataset")
        @Default.String("bq_dataset")
        String getBQDataset();

        void setBQDataset(String value);

//    *******    BQ Table Name   *******

        @Description("BigQuery Table")
        @Default.String("bq_table")
        String getBQTable();

        void setBQTable(String value);


//     *******   PubSub Subscription Name   *******

        @Description("Pubsub subscription")
        @Default.String("pubsub_subscription")
        String getSubscription();

        void setSubscription(String value);

//     *******   BigTable Instance Name   *******

        @Description("BigTable Instance")
        @Default.String("bt_instance")
        String getBTInstance();

        void setBTInstance(String value);

//     *******   BigTable Table Name   *******

        @Description("BigTable table")
        @Default.String("bt_table")
        String getBTTable();

        void setBTTable(String value);

//     *******   BQ Deadletter Table   *******

        @Description("BQ Deadletter table")
        @Default.String("bq_deadletter_tbl")
        String getOutputDeadletterTable();

        void setOutputDeadletterTable(String value);

//     *******   BT Lookup Key   *******

        @Description("BT Lookup Key")
        @Default.String("bt_lookup_key")
        String getBtLookupKey();

        void setBtLookupKey(String value);
    }

    public static void main(String[] args) {

        PipelineOptionsFactory.register(MyOptions.class);
        MyOptions options = PipelineOptionsFactory.fromArgs(args)
                .withValidation()
                .as(MyOptions.class);

        options.setRunner(DataflowRunner.class);
        options.setBQTable("sample_tbl_enriched");
        options.setBQDataset("df_sol_guide");
        options.setProjectId("dev-experiment-project");
        options.setBTInstance("df-sol-bt-instance");
        options.setBTTable("customer_profiles");
        options.setOutputDeadletterTable("deadletter_tbl");
        options.setSubscription("projects/dev-experiment-project/subscriptions/df-sol-pubsub-sub");
        options.setTempLocation("gs://df-sol-guide-gcs/tmp-loc");
        options.setRegion("us-central1");
        options.setBtLookupKey("bigtable_row_key");

        Pipeline p = Pipeline.create(options);

        final String PROJECT = options.getProjectId();
        final String SUBSCRIPTION = options.getSubscription();
        final String BQ_PROJECT = PROJECT;
        final String BQ_TABLE = options.getBQTable();
        final String BQ_DEADLETTER_TABLE = options.getOutputDeadletterTable();
        final String BQ_DATASET = options.getBQDataset();
        final String BT_INSTANCE = options.getBTInstance();
        final String BT_TABLE = options.getBTTable();
        final String BT_LOOKUP_KEY = options.getBtLookupKey();

        /*******     Read PubSub data  *********/
        PCollection<String> pubsubMessages = p
                .apply("ReadPubSubData", PubsubIO.readStrings().fromSubscription(SUBSCRIPTION));

        /*******     Count PubSub data using Metrics  *********/
        pubsubMessages.apply("CountPubSubData", ParDo.of(new DoFn<String, String>() {
            @ProcessElement
            public void processElement(ProcessContext c) {
                Metrics.pubsubMessages.inc();
            }
        }));

        /***
         *
         * TODO
         * Bigtable enrichment with pubsub data task is still pending.
         *
        PCollection<String> enrichedMessages = pubsubMessages.apply(
                "EnrichWithBigtable",
                ParDo.of(new BigTableEnrichment(PROJECT, BT_INSTANCE, BT_TABLE,BT_LOOKUP_KEY))
        );
        ***/


        /*******     Convert PubSub data to BigQuery Table Rows  *********/
        PCollectionTuple results = pubsubMessages.apply("TransformJSONToBQ", JsonToTableRows.run());

        PCollection<TableRow> windowedTableRows = results.get(SUCCESS_TAG)
                .apply("SessionWindow",
                        Window.<TableRow>into(Sessions.withGapDuration(Duration.standardMinutes(1))));

        /*******     Write Data to BigQuery Table  *********/
        WriteResult writeResult = windowedTableRows.apply("WriteSuccessfulRecordsToBQ", BigQueryIO.writeTableRows()
                .withMethod(BigQueryIO.Write.Method.STREAMING_INSERTS)
                .withFailedInsertRetryPolicy(InsertRetryPolicy.retryTransientErrors())
                .withWriteDisposition(WRITE_APPEND)
                .withCreateDisposition(CREATE_NEVER)
                .withExtendedErrorInfo()
                .ignoreUnknownValues()
                .skipInvalidRows()
                .withoutValidation()
                .to((row) -> {
                    return new TableDestination(String.format("%s:%s.%s",
                            BQ_PROJECT, BQ_DATASET, BQ_TABLE), "BQ Table destination");
                })
        );

        PCollection<FailsafeElement<String, String>> failedInserts =
                BigQueryIOUtils.writeResultToBigQueryInsertErrors(writeResult, options)
                        .apply(
                                "WrapInsertionErrors",
                                MapElements.into(FAILSAFE_ELEMENT_CODER.getEncodedTypeDescriptor())
                                        .via((BigQueryInsertError e) -> wrapBigQueryInsertError(e)))
                        .setCoder(FAILSAFE_ELEMENT_CODER);

        /*******     Failed insertion will be pushed to Bigquery Deadletter table  *********/
        failedInserts.apply("FailedRecordToTableRow", ParDo.of(new ErrorConverters.FailedStringToTableRowFn()))
                .apply(
                        "WriteFailedRecordsToBigQuery",
                        BigQueryIO.writeTableRows()
                                .to((row) -> {
                                    return new TableDestination(String.format("%s:%s.%s",
                                            BQ_PROJECT, BQ_DATASET, BQ_DEADLETTER_TABLE), "BQ Dead Letter Table destination");
                                })
                                .withJsonSchema(getDeadletterTableSchemaJson())
                                .withCreateDisposition(BigQueryIO.Write.CreateDisposition.CREATE_IF_NEEDED)
                                .withWriteDisposition(BigQueryIO.Write.WriteDisposition.WRITE_APPEND));
        p.run();
    }

    static FailsafeElement<String, String> wrapBigQueryInsertError(BigQueryInsertError insertError) {

        FailsafeElement<String, String> failsafeElement;
        try {

            String rowPayload = JSON_FACTORY.toString(insertError.getRow());
            String errorMessage = JSON_FACTORY.toString(insertError.getError());

            failsafeElement = FailsafeElement.of(rowPayload, rowPayload);
            failsafeElement.setErrorMessage(errorMessage);

        } catch (IOException e) {
            throw new RuntimeException(e);
        }

        return failsafeElement;
    }

    /*******     Read BigQuery Deadletter JSON Schema if table not exist  *********/
    static String getDeadletterTableSchemaJson() {
        String schemaJson = null;
        try {
            schemaJson =
                    Resources.toString(
                            Resources.getResource(DEADLETTER_SCHEMA_FILE_PATH), StandardCharsets.UTF_8);
        } catch (Exception e) {
            System.out.println("Unable to read {} file from the resources folder!");
        }

        return schemaJson;
    }
}