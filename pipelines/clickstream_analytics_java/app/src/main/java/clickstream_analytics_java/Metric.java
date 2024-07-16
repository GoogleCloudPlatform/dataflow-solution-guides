package clickstream_analytics_java;

import org.apache.beam.sdk.metrics.Counter;
import org.apache.beam.sdk.metrics.Metrics;

public final class Metric {
    public static Counter pubsubMessages = counter("pub-sub-messages");
    public static Counter successfulMessages = counter("successful-messages");
    public static Counter jsonParseErrorMessages = counter("json-parse-failed-messages");
    public static Counter tooBigMessages = counter("too-big-messages");
    public static Counter failedInsertMessages = counter("failed-insert-messages");
    static Counter counter(String name) {
        return Metrics.counter(Metric.class, name);
    }
}
