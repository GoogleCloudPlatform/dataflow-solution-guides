package clickstream_analytics_java;

import org.apache.beam.sdk.metrics.Counter;

public final class Metrics {
    public static Counter pubsubMessages = counter("pub-sub-messages");
    public static Counter successfulMessages = counter("successful-messages");
    public static Counter jsonParseErrorMessages = counter("json-parse-failed-messages");
    public static Counter tooBigMessages = counter("too-big-messages");
    public static Counter failedInsertMessages = counter("failed-insert-messages");
    static Counter counter(String name) {
        return org.apache.beam.sdk.metrics.Metrics.counter(Metrics.class, name);
    }
}
