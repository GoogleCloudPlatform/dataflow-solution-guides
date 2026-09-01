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
package com.google.cloud.dataflow.solutions.clickstream_analytics.extract;

import com.google.auto.value.AutoValue;
import org.apache.beam.sdk.io.gcp.pubsub.PubsubIO;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.values.PBegin;
import org.apache.beam.sdk.values.PCollection;

public final class PubSub {

    private PubSub() {}

    public static Read.Builder read() {
        return Read.builder();
    }

    public static Read fromSubscription(String subscription) {
        return Read.builder().withSubscription(subscription).build();
    }

    @AutoValue
    public abstract static class Read extends PTransform<PBegin, PCollection<String>> {

        public abstract String subscription();

        public static Builder builder() {
            return new AutoValue_PubSub_Read.Builder();
        }

        public Read withSubscription(String subscription) {
            return toBuilder().subscription(subscription).build();
        }

        public abstract Builder toBuilder();

        @AutoValue.Builder
        public abstract static class Builder {
            public abstract Builder subscription(String subscription);

            public Builder withSubscription(String subscription) {
                return subscription(subscription);
            }

            public abstract Read build();
        }

        @Override
        public PCollection<String> expand(PBegin input) {
            return input.apply(
                    "ReadFromSubscription",
                    PubsubIO.readStrings().fromSubscription(subscription()));
        }
    }
}
