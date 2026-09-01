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
package com.google.cloud.dataflow.solutions.clickstream_analytics.data;

import com.google.api.services.bigquery.model.TableRow;
import com.google.auto.value.AutoValue;
import java.io.Serializable;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import javax.annotation.Nullable;
import org.apache.beam.sdk.schemas.AutoValueSchema;
import org.apache.beam.sdk.schemas.annotations.DefaultSchema;
import org.apache.beam.sdk.schemas.annotations.SchemaFieldName;
import org.joda.time.Instant;

public final class ClickstreamObjects {

    private ClickstreamObjects() {}

    /** Represents a raw or enriched clickstream event. */
    @DefaultSchema(AutoValueSchema.class)
    @AutoValue
    public abstract static class ClickstreamEvent implements Serializable {

        @Nullable @SchemaFieldName("user_id")
        public abstract String getUserId();

        @Nullable @SchemaFieldName("timestamp")
        public abstract String getTimestamp();

        @Nullable @SchemaFieldName("prev")
        public abstract String getPrev();

        @Nullable @SchemaFieldName("curr")
        public abstract String getCurr();

        @Nullable @SchemaFieldName("type")
        public abstract String getType();

        @Nullable @SchemaFieldName("n")
        public abstract Integer getN();

        @Nullable @SchemaFieldName("category")
        public abstract String getCategory();

        @Nullable @SchemaFieldName("enriched_data")
        public abstract String getEnrichedData();

        public abstract Builder toBuilder();

        public static Builder builder() {
            return new AutoValue_ClickstreamObjects_ClickstreamEvent.Builder();
        }

        public TableRow toTableRow() {
            TableRow row = new TableRow();
            if (getUserId() != null) {
                row.set("user_id", getUserId());
            }
            if (getTimestamp() != null) {
                row.set("timestamp", getTimestamp());
            }
            if (getPrev() != null) {
                row.set("prev", getPrev());
            }
            if (getCurr() != null) {
                row.set("curr", getCurr());
            }
            if (getType() != null) {
                row.set("type", getType());
            }
            if (getN() != null) {
                row.set("n", getN());
            }
            if (getCategory() != null) {
                row.set("category", getCategory());
            }
            if (getEnrichedData() != null) {
                row.set("enriched_data", getEnrichedData());
            }
            return row;
        }

        @AutoValue.Builder
        public abstract static class Builder {
            public abstract Builder setUserId(@Nullable String value);

            public abstract Builder setTimestamp(@Nullable String value);

            public abstract Builder setPrev(@Nullable String value);

            public abstract Builder setCurr(@Nullable String value);

            public abstract Builder setType(@Nullable String value);

            public abstract Builder setN(@Nullable Integer value);

            public abstract Builder setCategory(@Nullable String value);

            public abstract Builder setEnrichedData(@Nullable String value);

            public abstract ClickstreamEvent build();
        }
    }

    /** Represents an aggregated user browsing session. */
    @DefaultSchema(AutoValueSchema.class)
    @AutoValue
    public abstract static class UserSession implements Serializable {

        @SchemaFieldName("session_id")
        public abstract String getSessionId();

        @Nullable @SchemaFieldName("user_id")
        public abstract String getUserId();

        @Nullable @SchemaFieldName("session_start")
        public abstract String getSessionStart();

        @Nullable @SchemaFieldName("session_end")
        public abstract String getSessionEnd();

        @Nullable @SchemaFieldName("duration_seconds")
        public abstract Double getDurationSeconds();

        @Nullable @SchemaFieldName("event_count")
        public abstract Integer getEventCount();

        @Nullable @SchemaFieldName("first_page")
        public abstract String getFirstPage();

        @Nullable @SchemaFieldName("last_page")
        public abstract String getLastPage();

        @Nullable @SchemaFieldName("unique_pages_count")
        public abstract Integer getUniquePagesCount();

        @Nullable @SchemaFieldName("total_views")
        public abstract Integer getTotalViews();

        public static Builder builder() {
            return new AutoValue_ClickstreamObjects_UserSession.Builder();
        }

        public TableRow toTableRow() {
            TableRow row = new TableRow();
            row.set("session_id", getSessionId());
            if (getUserId() != null) {
                row.set("user_id", getUserId());
            }
            if (getSessionStart() != null) {
                row.set("session_start", getSessionStart());
            }
            if (getSessionEnd() != null) {
                row.set("session_end", getSessionEnd());
            }
            if (getDurationSeconds() != null) {
                row.set("duration_seconds", getDurationSeconds());
            }
            if (getEventCount() != null) {
                row.set("event_count", getEventCount());
            }
            if (getFirstPage() != null) {
                row.set("first_page", getFirstPage());
            }
            if (getLastPage() != null) {
                row.set("last_page", getLastPage());
            }
            if (getUniquePagesCount() != null) {
                row.set("unique_pages_count", getUniquePagesCount());
            }
            if (getTotalViews() != null) {
                row.set("total_views", getTotalViews());
            }
            return row;
        }

        @AutoValue.Builder
        public abstract static class Builder {
            public abstract Builder setSessionId(String value);

            public abstract Builder setUserId(@Nullable String value);

            public abstract Builder setSessionStart(@Nullable String value);

            public abstract Builder setSessionEnd(@Nullable String value);

            public abstract Builder setDurationSeconds(@Nullable Double value);

            public abstract Builder setEventCount(@Nullable Integer value);

            public abstract Builder setFirstPage(@Nullable String value);

            public abstract Builder setLastPage(@Nullable String value);

            public abstract Builder setUniquePagesCount(@Nullable Integer value);

            public abstract Builder setTotalViews(@Nullable Integer value);

            public abstract UserSession build();
        }
    }

    /** Represents a parsing or validation error event. */
    @DefaultSchema(AutoValueSchema.class)
    @AutoValue
    public abstract static class ParsingError implements Serializable {

        @SchemaFieldName("input_data")
        public abstract String getInputData();

        @SchemaFieldName("error_message")
        public abstract String getErrorMessage();

        @SchemaFieldName("timestamp")
        public abstract Instant getTimestamp();

        public String getPayloadString() {
            return getInputData();
        }

        public TableRow toTableRow() {
            TableRow row = new TableRow();
            row.set(
                    "timestamp",
                    getTimestamp() != null ? getTimestamp().toString() : Instant.now().toString());
            String payload = getInputData() != null ? getInputData() : "";
            row.set("payloadString", payload);
            row.set("payloadBytes", payload.getBytes(StandardCharsets.UTF_8));
            row.set("attributes", Collections.emptyList());
            row.set("errorMessage", getErrorMessage() != null ? getErrorMessage() : "");
            row.set("stacktrace", "");
            return row;
        }

        public static Builder builder() {
            return new AutoValue_ClickstreamObjects_ParsingError.Builder();
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
