/*
*  Copyright 2024 Google LLC
*
*  Licensed under the Apache License, Version 2.0 (the "License");
*  you may not use this file except in compliance with the License.
*  You may obtain a copy of the License at
*
*      https://www.apache.org/licenses/LICENSE-2.0
*
*  Unless required by applicable law or agreed to in writing, software
*  distributed under the License is distributed on an "AS IS" BASIS,
*  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*  See the License for the specific language governing permissions and
*  limitations under the License.
*/

package com.google.cloud.dataflow.solutions.load;

import com.google.auto.value.AutoValue;
import javax.annotation.Nullable;
import org.apache.beam.sdk.io.gcp.pubsub.PubsubIO;
import org.apache.beam.sdk.io.gcp.spanner.changestreams.model.DataChangeRecord;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PDone;

public class Pubsub {
    @AutoValue
    public abstract static class Publisher
            extends PTransform<PCollection<DataChangeRecord>, PDone> {

        @Nullable public abstract String projectId();

        @Nullable public abstract Integer topicNumber();

        @Nullable public abstract String topicPrefix();

        abstract Builder toBuilder();

        public Publisher withTopicNumber(int topicNumber) {
            return toBuilder().topicNumber(topicNumber).build();
        }

        public Publisher withProjectId(String projectId) {
            return toBuilder().projectId(projectId).build();
        }

        public Publisher withTopicPrefix(String topicPrefix) {
            return toBuilder().topicPrefix(topicPrefix).build();
        }

        static Builder builder() {
            return new AutoValue_Pubsub_Publisher.Builder().topicPrefix("changestream");
        }

        public static Publisher publish() {
            return builder().build();
        }

        @AutoValue.Builder
        public abstract static class Builder {
            abstract Builder projectId(String projectId);

            abstract Builder topicNumber(Integer topicNumber);

            abstract Builder topicPrefix(String topicPrefix);

            abstract Publisher build();
        }

        @Override
        public PDone expand(PCollection<DataChangeRecord> input) {
            String topic =
                    String.format(
                            "projects/%s/topics/%s%d", projectId(), topicPrefix(), topicNumber());
            PCollection<String> asStrings = input.apply("ToString", ParDo.of(new RecordToString()));

            return asStrings.apply(
                    String.format("Write to Pubsub %d", topicNumber()),
                    PubsubIO.writeStrings().to(topic));
        }
    }

    private static class RecordToString extends DoFn<DataChangeRecord, String> {
        @ProcessElement
        public void processElement(@Element DataChangeRecord record, OutputReceiver<String> out) {
            out.output(record.toString());
        }
    }
}
