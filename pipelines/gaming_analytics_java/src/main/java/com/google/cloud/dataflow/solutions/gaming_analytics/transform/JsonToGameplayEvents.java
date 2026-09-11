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
package com.google.cloud.dataflow.solutions.gaming_analytics.transform;

import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.GameplayEvent;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.ProcessingError;
import org.apache.beam.sdk.metrics.Counter;
import org.apache.beam.sdk.metrics.Metrics;
import org.apache.beam.sdk.schemas.NoSuchSchemaException;
import org.apache.beam.sdk.schemas.Schema;
import org.apache.beam.sdk.schemas.transforms.Convert;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.Flatten;
import org.apache.beam.sdk.transforms.JsonToRow;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PCollectionList;
import org.apache.beam.sdk.values.PCollectionTuple;
import org.apache.beam.sdk.values.Row;
import org.apache.beam.sdk.values.TupleTag;
import org.apache.beam.sdk.values.TupleTagList;
import org.joda.time.Instant;

/**
 * Parses the raw JSON payloads into {@link GameplayEvent} elements and validates them.
 *
 * <p>Two kinds of failure are routed to {@link #ERROR_TAG} instead of failing the bundle:
 *
 * <ul>
 *   <li>{@code parse}: the payload is not JSON, or does not conform to the event schema.
 *   <li>{@code validate}: the payload is valid JSON but is missing the player id, or carries an
 *       event timestamp that cannot be interpreted.
 * </ul>
 */
public final class JsonToGameplayEvents extends PTransform<PCollection<String>, PCollectionTuple> {

    public static final TupleTag<GameplayEvent> SUCCESS_TAG =
            new TupleTag<GameplayEvent>("SUCCESS_TAG") {};
    public static final TupleTag<ProcessingError> ERROR_TAG =
            new TupleTag<ProcessingError>("ERROR_TAG") {};

    public static final String PARSE_STAGE = "parse";
    public static final String VALIDATE_STAGE = "validate";

    private JsonToGameplayEvents() {}

    public static JsonToGameplayEvents create() {
        return new JsonToGameplayEvents();
    }

    /**
     * Normalizes an event timestamp into an ISO-8601 UTC instant.
     *
     * @param timestamp the timestamp carried by the payload, possibly null or blank
     * @param fallback the timestamp to use when the payload does not carry one
     * @return the normalized instant
     * @throws IllegalArgumentException if the timestamp is present but cannot be parsed
     */
    public static Instant normalizeTimestamp(String timestamp, Instant fallback) {
        if (timestamp == null || timestamp.trim().isEmpty()) {
            return fallback;
        }
        try {
            return Instant.parse(timestamp.trim());
        } catch (IllegalArgumentException e) {
            throw new IllegalArgumentException(
                    String.format("Unparseable event_timestamp '%s'", timestamp), e);
        }
    }

    @Override
    public PCollectionTuple expand(PCollection<String> input) {
        Schema eventSchema;
        try {
            eventSchema = input.getPipeline().getSchemaRegistry().getSchema(GameplayEvent.class);
        } catch (NoSuchSchemaException e) {
            throw new IllegalStateException(
                    String.format("No schema found for GameplayEvent class: %s", e.getMessage()),
                    e);
        }

        JsonToRow.ParseResult parseResult =
                input.apply(
                        "Json2Row",
                        JsonToRow.withExceptionReporting(eventSchema).withExtendedErrorInfo());

        PCollection<ProcessingError> parseErrors =
                parseResult
                        .getFailedToParseLines()
                        .apply("FailedLinesToErrors", ParDo.of(new FailedLineToErrorDoFn()));

        PCollectionTuple validated =
                parseResult
                        .getResults()
                        .apply("Row2GameplayEvent", Convert.fromRows(GameplayEvent.class))
                        .apply(
                                "ValidateEvents",
                                ParDo.of(new ValidateEventDoFn())
                                        .withOutputTags(SUCCESS_TAG, TupleTagList.of(ERROR_TAG)));

        PCollection<ProcessingError> allErrors =
                PCollectionList.of(parseErrors)
                        .and(validated.get(ERROR_TAG))
                        .apply("FlattenParseErrors", Flatten.pCollections());

        return PCollectionTuple.of(SUCCESS_TAG, validated.get(SUCCESS_TAG))
                .and(ERROR_TAG, allErrors);
    }

    /** Maps the rows rejected by {@link JsonToRow} into dead-letter records. */
    private static class FailedLineToErrorDoFn extends DoFn<Row, ProcessingError> {
        private final Counter parseErrors =
                Metrics.counter(JsonToGameplayEvents.class, "json-parse-errors");

        @ProcessElement
        public void processElement(
                @Element Row row,
                @Timestamp Instant timestamp,
                OutputReceiver<ProcessingError> output) {
            parseErrors.inc();
            output.output(
                    ProcessingError.of(
                            PARSE_STAGE,
                            row.getString("line"),
                            row.getString("err"),
                            timestamp.toString()));
        }
    }

    /** Enforces the business invariants the downstream transforms and BigQuery rely on. */
    private static class ValidateEventDoFn extends DoFn<GameplayEvent, GameplayEvent> {
        private final Counter validationErrors =
                Metrics.counter(JsonToGameplayEvents.class, "event-validation-errors");
        private final Counter validEvents =
                Metrics.counter(JsonToGameplayEvents.class, "valid-events");

        @ProcessElement
        public void processElement(
                @Element GameplayEvent event,
                @Timestamp Instant timestamp,
                MultiOutputReceiver output) {
            if (event.getPlayerId() == null || event.getPlayerId().trim().isEmpty()) {
                validationErrors.inc();
                output.get(ERROR_TAG)
                        .output(
                                ProcessingError.of(
                                        VALIDATE_STAGE,
                                        event.toString(),
                                        "Missing mandatory field player_id",
                                        timestamp.toString()));
                return;
            }

            Instant eventTime;
            try {
                eventTime = normalizeTimestamp(event.getEventTimestamp(), timestamp);
            } catch (IllegalArgumentException e) {
                validationErrors.inc();
                output.get(ERROR_TAG)
                        .output(
                                ProcessingError.of(
                                        VALIDATE_STAGE,
                                        event.toString(),
                                        e.getMessage(),
                                        timestamp.toString()));
                return;
            }

            validEvents.inc();
            output.get(SUCCESS_TAG)
                    .output(event.toBuilder().setEventTimestamp(eventTime.toString()).build());
        }
    }
}
