package clickstream_analytics_java;

import com.google.api.services.bigquery.model.TableRow;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;
import org.apache.beam.sdk.io.gcp.pubsub.PubsubMessage;
import org.apache.beam.sdk.metrics.Counter;
import org.apache.beam.sdk.metrics.Metrics;
import org.apache.beam.sdk.transforms.DoFn;
import org.joda.time.DateTimeZone;
import org.joda.time.format.DateTimeFormat;
import org.joda.time.format.DateTimeFormatter;

public class ErrorConverters {

    public static class FailedStringToTableRowFn
            extends DoFn<FailsafeElement<String, String>, TableRow> {

        /**
         * The formatter used to convert timestamps into a BigQuery compatible <a
         * href="https://cloud.google.com/bigquery/docs/reference/standard-sql/data-types#timestamp-type">format</a>.
         */
        private static final DateTimeFormatter TIMESTAMP_FORMATTER =
                DateTimeFormat.forPattern("yyyy-MM-dd HH:mm:ss.SSSSSS");

        @ProcessElement
        public void processElement(ProcessContext context) {
            FailsafeElement<String, String> failsafeElement = context.element();
            String message = failsafeElement.getOriginalPayload();

            // Format the timestamp for insertion
            String timestamp =
                    TIMESTAMP_FORMATTER.print(context.timestamp().toDateTime(DateTimeZone.UTC));

            // Build the table row
            TableRow failedRow =
                    new TableRow()
                            .set("timestamp", timestamp)
                            .set("errorMessage", failsafeElement.getErrorMessage())
                            .set("stacktrace", failsafeElement.getStacktrace());

            // Only set the payload if it's populated on the message.
            if (message != null) {
                failedRow
                        .set("payloadString", message)
                        .set("payloadBytes", message.getBytes(StandardCharsets.UTF_8));
            }

            context.output(failedRow);
        }
    }

    protected static class FailedStringToPubsubMessageFn
            extends DoFn<FailsafeElement<String, String>, PubsubMessage> {

        protected static final String ERROR_MESSAGE = "errorMessage";
        protected static final String TIMESTAMP = "timestamp";
        protected static final DateTimeFormatter TIMESTAMP_FORMATTER =
                DateTimeFormat.forPattern("yyyy-MM-dd HH:mm:ss.SSSSSS");

        private static final Counter ERROR_MESSAGES_COUNTER =
                Metrics.counter(FailedStringToPubsubMessageFn.class, "total-failed-messages");

        @ProcessElement
        public void processElement(ProcessContext context) {
            FailsafeElement<String, String> failsafeElement = context.element();

            String message = failsafeElement.getOriginalPayload();

            // Format the timestamp for insertion
            String timestamp =
                    TIMESTAMP_FORMATTER.print(context.timestamp().toDateTime(DateTimeZone.UTC));

            Map<String, String> attributes = new HashMap<>();
            attributes.put(TIMESTAMP, timestamp);

            if (failsafeElement.getErrorMessage() != null) {
                attributes.put(ERROR_MESSAGE, failsafeElement.getErrorMessage());
            }

            PubsubMessage pubsubMessage = new PubsubMessage(message.getBytes(), attributes);

            ERROR_MESSAGES_COUNTER.inc();

            context.output(pubsubMessage);
        }
    }
}
