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
