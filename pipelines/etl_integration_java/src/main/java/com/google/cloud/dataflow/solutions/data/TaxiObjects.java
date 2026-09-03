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

public class TaxiObjects {

    /** Represents Taxi Ride Event */
    @DefaultSchema(AutoValueSchema.class)
    @AutoValue
    public abstract static class TaxiEvent {

        @SchemaFieldName("ride_id")
        public abstract String getRideId();

        @SchemaFieldName("point_idx")
        public abstract int getPointIdx();

        @SchemaFieldName("latitude")
        public abstract double getLatitude();

        @SchemaFieldName("longitude")
        public abstract double getLongitude();

        @SchemaFieldName("timestamp")
        public abstract String getTimeStamp();

        @SchemaFieldName("meter_reading")
        public abstract double getMeterReading();

        @SchemaFieldName("meter_increment")
        public abstract double getMeterIncrement();

        @SchemaFieldName("ride_status")
        public abstract String getRideStatus();

        @SchemaFieldName("passenger_count")
        public abstract int getPassengerCount();

        public static Builder builder() {
            return new AutoValue_TaxiObjects_TaxiEvent.Builder();
        }

        @AutoValue.Builder
        public abstract static class Builder {
            public abstract Builder setRideId(String value);

            public abstract Builder setPointIdx(int value);

            public abstract Builder setLatitude(double latitude);

            public abstract Builder setLongitude(double longitude);

            public abstract Builder setTimeStamp(String value);

            public abstract Builder setMeterReading(double value);

            public abstract Builder setMeterIncrement(double value);

            public abstract Builder setRideStatus(String value);

            public abstract Builder setPassengerCount(int value);

            public abstract TaxiEvent build();
        }
    }
}
