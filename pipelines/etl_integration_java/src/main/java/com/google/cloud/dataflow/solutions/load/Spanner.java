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
import com.google.cloud.dataflow.solutions.data.TaxiObjects;
import com.google.cloud.spanner.Mutation;
import org.apache.beam.sdk.io.gcp.spanner.SpannerIO;
import org.apache.beam.sdk.io.gcp.spanner.SpannerWriteResult;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.PCollection;

public class Spanner {

    @AutoValue
    public abstract static class Writer
            extends PTransform<PCollection<TaxiObjects.TaxiEvent>, SpannerWriteResult> {

        public abstract String projectId();

        public abstract String instanceId();

        public abstract String databaseId();

        public abstract String tableName();

        public static Writer.Builder builder() {
            return new AutoValue_Spanner_Writer.Builder();
        }

        @AutoValue.Builder
        public abstract static class Builder {
            public abstract Builder projectId(String projectId);

            public abstract Builder instanceId(String instanceId);

            public abstract Builder databaseId(String databaseId);

            public abstract Builder tableName(String tableName);

            public abstract Writer build();
        }

        @Override
        public SpannerWriteResult expand(PCollection<TaxiObjects.TaxiEvent> events) {
            PCollection<Mutation> mutations =
                    events.apply("To mutations", ParDo.of(new EventToMutationDoFn(tableName())));

            return mutations.apply(
                    "Write to Spanner",
                    SpannerIO.write()
                            .withProjectId(projectId())
                            .withInstanceId(instanceId())
                            .withDatabaseId(databaseId()));
        }
    }

    private static class EventToMutationDoFn extends DoFn<TaxiObjects.TaxiEvent, Mutation> {
        private final String tableName;

        public EventToMutationDoFn(String tableName) {
            this.tableName = tableName;
        }

        @ProcessElement
        public void processElement(
                @Element TaxiObjects.TaxiEvent e, OutputReceiver<Mutation> receiver) {
            Mutation m =
                    Mutation.newInsertOrUpdateBuilder(tableName)
                            .set("ride_id")
                            .to(e.getRideId())
                            .set("point_idx")
                            .to(e.getPointIdx())
                            .set("latitude")
                            .to(e.getLatitude())
                            .set("longitude")
                            .to(e.getLongitude())
                            .set("timestamp")
                            .to(com.google.cloud.Timestamp.parseTimestamp(e.getTimeStamp()))
                            .set("meter_reading")
                            .to(e.getMeterReading())
                            .set("meter_increment")
                            .to(e.getMeterIncrement())
                            .set("ride_status")
                            .to(e.getRideStatus())
                            .set("passenger_count")
                            .to(e.getPassengerCount())
                            .build();

            receiver.output(m);
        }
    }
}
