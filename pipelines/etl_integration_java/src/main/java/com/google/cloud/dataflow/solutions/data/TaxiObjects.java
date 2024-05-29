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

package com.google.cloud.dataflow.solutions.data;

import com.google.auto.value.AutoValue;
import org.apache.beam.sdk.schemas.AutoValueSchema;
import org.apache.beam.sdk.schemas.annotations.DefaultSchema;
import org.apache.beam.sdk.schemas.annotations.SchemaFieldName;
import org.joda.time.Instant;

public class TaxiObjects {
    /** Represents Taxi Ride Event */
    @DefaultSchema(AutoValueSchema.class)
    @AutoValue
    public abstract static class TaxiEvent {

        @SchemaFieldName("ride_id")
        public abstract String getRideId();

        @SchemaFieldName("point_idx")
        public abstract Integer getPointIdx();

        @SchemaFieldName("latitude")
        public abstract Double getLatitude();

        @SchemaFieldName("longitude")
        public abstract Double getLongitude();

        @SchemaFieldName("timestamp")
        public abstract String getTimeStamp();

        @SchemaFieldName("meter_reading")
        public abstract Double getMeterReading();

        @SchemaFieldName("meter_increment")
        public abstract Double getMeterIncrement();

        @SchemaFieldName("ride_status")
        public abstract String getRideStatus();

        @SchemaFieldName("passenger_count")
        public abstract Integer getPassengerCount();

        public abstract Builder toBuilder();

        public static Builder builder() {
            return new AutoValue_TaxiObjects_TaxiEvent.Builder();
        }

        @AutoValue.Builder
        public abstract static class Builder {

            public abstract Builder setRideId(String value);

            public abstract Builder setPointIdx(Integer value);

            public abstract Builder setLatitude(Double value);

            public abstract Builder setLongitude(Double value);

            public abstract Builder setTimeStamp(String value);

            public abstract Builder setMeterReading(Double value);

            public abstract Builder setMeterIncrement(Double value);

            public abstract Builder setRideStatus(String value);

            public abstract Builder setPassengerCount(Integer value);

            public abstract TaxiEvent build();
        }
    }

    @AutoValue
    @DefaultSchema(AutoValueSchema.class)
    /* Represents a parsing error message event */
    public abstract static class ParsingError {

        @SchemaFieldName("inputData")
        public abstract String getInputData();

        @SchemaFieldName("errorMessage")
        public abstract String getErrorMessage();

        @SchemaFieldName("timestamp")
        public abstract Instant getTimestamp();

        public abstract Builder toBuilder();

        public static Builder builder() {
            return new AutoValue_TaxiObjects_ParsingError.Builder();
        }

        @AutoValue.Builder
        public abstract static class Builder {

            public abstract Builder setInputData(String value);

            public abstract Builder setErrorMessage(String value);

            public abstract Builder setTimestamp(Instant value);

            public abstract ParsingError build();
        }
    }
}
