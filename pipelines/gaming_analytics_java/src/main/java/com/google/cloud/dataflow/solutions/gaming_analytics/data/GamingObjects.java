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
package com.google.cloud.dataflow.solutions.gaming_analytics.data;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.google.api.services.bigquery.model.TableRow;
import com.google.auto.value.AutoValue;
import java.io.Serializable;
import java.util.Map;
import javax.annotation.Nullable;
import org.apache.beam.sdk.schemas.AutoValueSchema;
import org.apache.beam.sdk.schemas.annotations.DefaultSchema;
import org.apache.beam.sdk.schemas.annotations.SchemaFieldName;

/** Data objects flowing through the gaming analytics pipeline. */
public final class GamingObjects {

    private GamingObjects() {}

    private static final ObjectMapper MAPPER = new ObjectMapper();

    /**
     * A raw gameplay event, as published by the gaming platform servers to the input Pub/Sub topic.
     *
     * <p>Every field is {@link Nullable} because this class is the schema source used by {@code
     * JsonToRow.withExceptionReporting(...)}: making a field mandatory at the schema level would
     * turn a missing optional attribute into an unparseable message. Mandatory business fields
     * (currently {@code player_id}) are instead validated explicitly by {@code
     * JsonToGameplayEvents}, so that the offending payload can be routed to the dead-letter topic
     * with a meaningful error message.
     */
    @DefaultSchema(AutoValueSchema.class)
    @AutoValue
    public abstract static class GameplayEvent implements Serializable {

        @Nullable @SchemaFieldName("player_id")
        public abstract String getPlayerId();

        @Nullable @SchemaFieldName("session_id")
        public abstract String getSessionId();

        @Nullable @SchemaFieldName("event_type")
        public abstract String getEventType();

        @Nullable @SchemaFieldName("level")
        public abstract Integer getLevel();

        @Nullable @SchemaFieldName("score")
        public abstract Long getScore();

        /** Event time, as an ISO-8601 string (for example {@code 2026-09-11T09:53:50.000Z}). */
        @Nullable @SchemaFieldName("event_timestamp")
        public abstract String getEventTimestamp();

        public abstract Builder toBuilder();

        public static Builder builder() {
            return new AutoValue_GamingObjects_GameplayEvent.Builder();
        }

        /** Builder for {@link GameplayEvent}. */
        @AutoValue.Builder
        public abstract static class Builder {
            public abstract Builder setPlayerId(@Nullable String value);

            public abstract Builder setSessionId(@Nullable String value);

            public abstract Builder setEventType(@Nullable String value);

            public abstract Builder setLevel(@Nullable Integer value);

            public abstract Builder setScore(@Nullable Long value);

            public abstract Builder setEventTimestamp(@Nullable String value);

            public abstract GameplayEvent build();
        }
    }

    /**
     * A gameplay event hydrated with the player features read from the Cloud Bigtable feature
     * store.
     *
     * <p>{@code features} is a flat map of Bigtable qualifier to the latest cell value, decoded as
     * UTF-8. It is never null: a feature store miss yields an empty map so that inference can still
     * run with defaults.
     */
    @DefaultSchema(AutoValueSchema.class)
    @AutoValue
    public abstract static class EnrichedEvent implements Serializable {

        @SchemaFieldName("event")
        public abstract GameplayEvent getEvent();

        @SchemaFieldName("features")
        public abstract Map<String, String> getFeatures();

        public static EnrichedEvent of(GameplayEvent event, Map<String, String> features) {
            return builder().setEvent(event).setFeatures(features).build();
        }

        public abstract Builder toBuilder();

        public static Builder builder() {
            return new AutoValue_GamingObjects_EnrichedEvent.Builder();
        }

        /** Builder for {@link EnrichedEvent}. */
        @AutoValue.Builder
        public abstract static class Builder {
            public abstract Builder setEvent(GameplayEvent value);

            public abstract Builder setFeatures(Map<String, String> value);

            public abstract EnrichedEvent build();
        }
    }

    /**
     * A scored gameplay event: the in-game activation published to the output Pub/Sub topic and the
     * analytical record persisted in BigQuery.
     *
     * <p>The field names match the columns of the {@code player_recommendations} table created by
     * the Terraform module of this solution guide.
     */
    @DefaultSchema(AutoValueSchema.class)
    @AutoValue
    public abstract static class Recommendation implements Serializable {

        @SchemaFieldName("player_id")
        public abstract String getPlayerId();

        @Nullable @SchemaFieldName("session_id")
        public abstract String getSessionId();

        @Nullable @SchemaFieldName("event_type")
        public abstract String getEventType();

        @Nullable @SchemaFieldName("level")
        public abstract Integer getLevel();

        @Nullable @SchemaFieldName("score")
        public abstract Long getScore();

        @Nullable @SchemaFieldName("recommendation")
        public abstract String getRecommendation();

        @Nullable @SchemaFieldName("recommendation_score")
        public abstract Double getRecommendationScore();

        @SchemaFieldName("event_timestamp")
        public abstract String getEventTimestamp();

        @Nullable @SchemaFieldName("processing_timestamp")
        public abstract String getProcessingTimestamp();

        public abstract Builder toBuilder();

        public static Builder builder() {
            return new AutoValue_GamingObjects_Recommendation.Builder();
        }

        /** Converts this recommendation into a BigQuery row. */
        public TableRow toTableRow() {
            TableRow row = new TableRow();
            row.set("player_id", getPlayerId());
            row.set("event_timestamp", getEventTimestamp());
            if (getSessionId() != null) {
                row.set("session_id", getSessionId());
            }
            if (getEventType() != null) {
                row.set("event_type", getEventType());
            }
            if (getLevel() != null) {
                row.set("level", getLevel());
            }
            if (getScore() != null) {
                row.set("score", getScore());
            }
            if (getRecommendation() != null) {
                row.set("recommendation", getRecommendation());
            }
            if (getRecommendationScore() != null) {
                row.set("recommendation_score", getRecommendationScore());
            }
            if (getProcessingTimestamp() != null) {
                row.set("processing_timestamp", getProcessingTimestamp());
            }
            return row;
        }

        /** Serializes this recommendation as the JSON payload published to Pub/Sub. */
        public String toJsonString() {
            ObjectNode node = MAPPER.createObjectNode();
            node.put("player_id", getPlayerId());
            node.put("session_id", getSessionId());
            node.put("event_type", getEventType());
            if (getLevel() != null) {
                node.put("level", getLevel());
            } else {
                node.putNull("level");
            }
            if (getScore() != null) {
                node.put("score", getScore());
            } else {
                node.putNull("score");
            }
            node.put("recommendation", getRecommendation());
            if (getRecommendationScore() != null) {
                node.put("recommendation_score", getRecommendationScore());
            } else {
                node.putNull("recommendation_score");
            }
            node.put("event_timestamp", getEventTimestamp());
            node.put("processing_timestamp", getProcessingTimestamp());
            return node.toString();
        }

        /** Builder for {@link Recommendation}. */
        @AutoValue.Builder
        public abstract static class Builder {
            public abstract Builder setPlayerId(String value);

            public abstract Builder setSessionId(@Nullable String value);

            public abstract Builder setEventType(@Nullable String value);

            public abstract Builder setLevel(@Nullable Integer value);

            public abstract Builder setScore(@Nullable Long value);

            public abstract Builder setRecommendation(@Nullable String value);

            public abstract Builder setRecommendationScore(@Nullable Double value);

            public abstract Builder setEventTimestamp(String value);

            public abstract Builder setProcessingTimestamp(@Nullable String value);

            public abstract Recommendation build();
        }
    }

    /**
     * An element the pipeline could not process, routed to the dead-letter Pub/Sub topic instead of
     * being dropped.
     */
    @DefaultSchema(AutoValueSchema.class)
    @AutoValue
    public abstract static class ProcessingError implements Serializable {

        /** Pipeline stage that produced the failure: parse, validate, enrich, inference or sink. */
        @SchemaFieldName("stage")
        public abstract String getStage();

        /** Best-effort textual representation of the element that could not be processed. */
        @SchemaFieldName("payload")
        public abstract String getPayload();

        @SchemaFieldName("error_message")
        public abstract String getErrorMessage();

        /** Time at which the failure was detected, as an ISO-8601 string. */
        @SchemaFieldName("timestamp")
        public abstract String getTimestamp();

        public static ProcessingError of(
                String stage, String payload, String errorMessage, String timestamp) {
            return builder()
                    .setStage(stage)
                    .setPayload(payload != null ? payload : "")
                    .setErrorMessage(errorMessage != null ? errorMessage : "")
                    .setTimestamp(timestamp)
                    .build();
        }

        public static Builder builder() {
            return new AutoValue_GamingObjects_ProcessingError.Builder();
        }

        /** Serializes this error as the JSON payload published to the dead-letter topic. */
        public String toJsonString() {
            ObjectNode node = MAPPER.createObjectNode();
            node.put("stage", getStage());
            node.put("payload", getPayload());
            node.put("error_message", getErrorMessage());
            node.put("timestamp", getTimestamp());
            return node.toString();
        }

        /** Parses a dead-letter payload back into a {@link ProcessingError}. Used by tests. */
        public static ProcessingError fromJsonString(String json) throws JsonProcessingException {
            ObjectNode node = (ObjectNode) MAPPER.readTree(json);
            return of(
                    node.path("stage").asText(),
                    node.path("payload").asText(),
                    node.path("error_message").asText(),
                    node.path("timestamp").asText());
        }

        /** Builder for {@link ProcessingError}. */
        @AutoValue.Builder
        public abstract static class Builder {
            public abstract Builder setStage(String value);

            public abstract Builder setPayload(String value);

            public abstract Builder setErrorMessage(String value);

            public abstract Builder setTimestamp(String value);

            public abstract ProcessingError build();
        }
    }
}
