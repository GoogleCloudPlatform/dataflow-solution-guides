package clickstream_analytics_java;

import java.util.*;
import org.joda.time.DateTime;
import org.joda.time.format.DateTimeFormat;
import org.joda.time.format.DateTimeFormatter;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import org.joda.time.Duration;

import org.apache.beam.sdk.Pipeline;

import org.apache.beam.sdk.options.PipelineOptionsFactory;
import org.apache.beam.sdk.options.Description;
import org.apache.beam.sdk.options.Default;

import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.transforms.Contextful;
import org.apache.beam.sdk.transforms.windowing.*;
import org.apache.beam.sdk.transforms.Flatten;

import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PCollectionTuple;
import org.apache.beam.sdk.values.PCollectionList;
import org.apache.beam.sdk.values.KV;

import org.apache.beam.runners.dataflow.options.DataflowPipelineOptions;
import org.apache.beam.runners.direct.DirectOptions;

import org.apache.beam.sdk.io.gcp.pubsub.PubsubIO;
import org.apache.beam.sdk.io.TextIO;
import org.apache.beam.sdk.io.FileIO;
import org.apache.beam.sdk.io.Compression;

import org.apache.beam.sdk.io.gcp.bigquery.BigQueryIO;
import org.apache.beam.sdk.io.gcp.bigquery.TableDestination;
import org.apache.beam.sdk.io.gcp.bigquery.InsertRetryPolicy;
import org.apache.beam.sdk.io.gcp.bigquery.WriteResult;
import static org.apache.beam.sdk.io.gcp.bigquery.BigQueryIO.Write.WriteDisposition.WRITE_APPEND;
import static org.apache.beam.sdk.io.gcp.bigquery.BigQueryIO.Write.CreateDisposition.CREATE_NEVER;

import org.apache.beam.sdk.coders.StringUtf8Coder;

import org.apache.beam.sdk.io.gcp.bigquery.BigQueryInsertError;
import org.apache.beam.sdk.transforms.MapElements;
import org.apache.beam.sdk.transforms.SimpleFunction;

import static clickstream_analytics_java.JsonToBQ.SUCCESS_TAG;
import static clickstream_analytics_java.JsonToBQ.FAILURE_TAG;

public class App {

    public interface MyOptions extends DataflowPipelineOptions, DirectOptions {
        @Description("BigQuery project")
        @Default.String("my_bq_project")
        String getBQProject();

        void setBQProject(String value);

        @Description("BigQuery dataset")
        @Default.String("my_bq_dataset")
        String getBQDataset();

        void setBQDataset(String value);

        @Description("Bucket path to collect pipeline errors in json files")
        @Default.String("errors")

        String getErrorsBucket();
        void setErrorsBucket(String value);

        @Description("Bucket path to collect pipeline errors in json files")
        @Default.String("my_bucket")
        String getBucket();

        void setBucket(String value);

        @Description("Pubsub project")
        @Default.String("my_pubsub_project")
        String getPubSubProject();

        void setPubSubProject(String value);

        @Description("Pubsub subscription")
        @Default.String("my_pubsub_subscription")
        String getSubscription();

        void setSubscription(String value);
    }

    private static final Logger LOG = LoggerFactory.getLogger(App.class);

    /**
     * class {@link ErrorFormatFileName} implement file naming format for files in errors bucket (failed rows)
     * used in withNaming
     */
    private static class ErrorFormatFileName implements FileIO.Write.FileNaming {

        private static final DateTimeFormatter DATE_FORMAT = DateTimeFormat.forPattern("yyyy-MM-dd");
        private static final DateTimeFormatter TIME_FORMAT = DateTimeFormat.forPattern("HH:mm:ss");

        private final String filePrefix;

        ErrorFormatFileName(String prefix) {
            filePrefix = prefix;
        }

        /**
         * Create filename in specific format
         * @return A string representing filename.
         */
        @Override
        public String getFilename(BoundedWindow window, PaneInfo pane, int numShards, int shardIndex, Compression compression) {
            return String.format(
                    "%s/%s/error-%s-%s-of-%s%s",
                    filePrefix,
                    DATE_FORMAT.print(new DateTime()),
                    TIME_FORMAT.print(new DateTime()),
                    shardIndex,
                    numShards,
                    compression.getSuggestedSuffix());
        }
    }

    public static void main(String[] args) {

        PipelineOptionsFactory.register(MyOptions.class);
        MyOptions options = PipelineOptionsFactory.fromArgs(args)
                .withValidation()
                .as(MyOptions.class);
        Pipeline p = Pipeline.create(options);

        final String PROJECT = options.getProject();
        final String ERRORS_BUCKET = String.format("gs://%s/%s/", options.getBucket(), options.getErrorsBucket());
        final String SUBSCRIPTION = String.format("projects/%s/subscriptions/%s", options.getPubSubProject(), options.getSubscription());
        final int STORAGE_LOAD_INTERVAL = 1;
        final int STORAGE_NUM_SHARDS = 1;

        final String BQ_PROJECT = options.getBQProject();
        final String BQ_DATASET = options.getBQDataset();

        PCollection<String> pubsubMessages = p
                .apply("ReadPubSubSubscription", PubsubIO.readStrings().fromSubscription(SUBSCRIPTION));

        pubsubMessages.apply("CountPubSubData", ParDo.of(new DoFn<String, String>() {
            @ProcessElement
            public void processElement(ProcessContext c)  {
                Metric.pubsubMessages.inc();
            }
        }));

        PCollectionTuple results = pubsubMessages.apply("TransformJSONToBQ", JsonToBQ.run());

        WriteResult writeResult = results.get(SUCCESS_TAG).apply("WriteSuccessfulRecordsToBQ", BigQueryIO.writeTableRows()
                .withMethod(BigQueryIO.Write.Method.STREAMING_INSERTS)
                .withFailedInsertRetryPolicy(InsertRetryPolicy.retryTransientErrors())
                .withWriteDisposition(WRITE_APPEND)
                .withCreateDisposition(CREATE_NEVER)
                .withExtendedErrorInfo()
                .ignoreUnknownValues()
                .skipInvalidRows()
                .withoutValidation()
                .to((row) -> {
                    String tableName = Objects.requireNonNull(row.getValue()).get("event_type").toString();
                    return new TableDestination(String.format("%s:%s.%s", BQ_PROJECT, BQ_DATASET, tableName), "Some destination");
                })
        );

        PCollection<KV<String, String>> failedInserts = writeResult.getFailedInsertsWithErr()
                .apply("MapFailedInserts", MapElements.via(new SimpleFunction<BigQueryInsertError, KV<String, String>>() {
                                                               @Override
                                                               public KV<String, String> apply(BigQueryInsertError input) {
                                                                   return KV.of("FailedInserts", input.getError().toString() + " for table" + input.getRow().get("table") + ", message: "+ input.getRow().toString());
                                                               }
                                                           }
                ));

        failedInserts.apply("LogFailedInserts", ParDo.of(new DoFn<KV<String, String>, Void>() {
            @ProcessElement
            public void processElement(ProcessContext c)  {
                LOG.error("{}: {}", c.element().getKey(), c.element().getValue());
                Metric.failedInsertMessages.inc();
            }
        }));

        PCollectionList<KV<String, String>> allErrors = PCollectionList.of(results.get(FAILURE_TAG)).and(failedInserts);
        allErrors.apply(Flatten.<KV<String, String>>pCollections())
                .apply("Window Errors", Window.<KV<String, String>>into(new GlobalWindows())
                        .triggering(Repeatedly
                                .forever(AfterProcessingTime
                                        .pastFirstElementInPane()
                                        .plusDelayOf(Duration.standardMinutes(STORAGE_LOAD_INTERVAL)))
                        )
                        .withAllowedLateness(Duration.standardDays(1))
                        .discardingFiredPanes()
                )
                .apply("WriteErrorsToGCS", FileIO.<String, KV<String, String>>writeDynamic()
                        .withDestinationCoder(StringUtf8Coder.of())
                        .by(KV::getKey)
                        .via(Contextful.fn(KV::getValue), TextIO.sink())
                        .withNumShards(STORAGE_NUM_SHARDS)
                        .to(ERRORS_BUCKET)
                        .withNaming(ErrorFormatFileName::new));
        p.run();
    }
}