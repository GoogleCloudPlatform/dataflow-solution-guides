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

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.GameplayEvent;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.ProcessingError;
import java.io.Serializable;
import java.util.Arrays;
import org.apache.beam.sdk.testing.PAssert;
import org.apache.beam.sdk.testing.TestPipeline;
import org.apache.beam.sdk.transforms.Create;
import org.apache.beam.sdk.values.PCollectionTuple;
import org.joda.time.Instant;
import org.junit.Rule;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.JUnit4;

@RunWith(JUnit4.class)
public class JsonToGameplayEventsTest implements Serializable {

    @Rule public final transient TestPipeline pipeline = TestPipeline.create();

    private static final String VALID_EVENT =
            "{\"player_id\": \"player_0001\", \"session_id\": \"session_1\","
                    + " \"event_type\": \"level_failed\", \"level\": 12, \"score\": 9100,"
                    + " \"event_timestamp\": \"2026-09-11T09:53:50.000Z\"}";

    @Test
    public void testNormalizeTimestampUsesFallbackWhenAbsent() {
        Instant fallback = Instant.parse("2026-01-01T00:00:00.000Z");

        assertEquals(fallback, JsonToGameplayEvents.normalizeTimestamp(null, fallback));
        assertEquals(fallback, JsonToGameplayEvents.normalizeTimestamp("", fallback));
        assertEquals(fallback, JsonToGameplayEvents.normalizeTimestamp("   ", fallback));
    }

    @Test
    public void testNormalizeTimestampParsesIso8601() {
        Instant fallback = Instant.parse("2026-01-01T00:00:00.000Z");

        assertEquals(
                Instant.parse("2026-09-11T09:53:50.000Z"),
                JsonToGameplayEvents.normalizeTimestamp("2026-09-11T09:53:50Z", fallback));
        // An offset is honoured rather than ignored.
        assertEquals(
                Instant.parse("2026-09-11T07:53:50.000Z"),
                JsonToGameplayEvents.normalizeTimestamp("2026-09-11T09:53:50+02:00", fallback));
    }

    @Test
    public void testNormalizeTimestampParsesTheGeneratorFormat() {
        // scripts/generate_gameplay_events.py stamps events with
        // datetime.datetime.now(datetime.timezone.utc).isoformat(), which yields microsecond
        // precision and a numeric '+00:00' offset rather than a 'Z'. If this were rejected, every
        // event published by the guide's own generator would be dead-lettered.
        Instant fallback = Instant.parse("2026-01-01T00:00:00.000Z");

        assertEquals(
                Instant.parse("2026-09-11T09:53:50.123Z"),
                JsonToGameplayEvents.normalizeTimestamp(
                        "2026-09-11T09:53:50.123456+00:00", fallback));
    }

    @Test
    public void testNormalizeTimestampRejectsGarbage() {
        Instant fallback = Instant.parse("2026-01-01T00:00:00.000Z");

        IllegalArgumentException thrown =
                assertThrows(
                        IllegalArgumentException.class,
                        () -> JsonToGameplayEvents.normalizeTimestamp("yesterday", fallback));
        assertTrue(thrown.getMessage().contains("yesterday"));
    }

    @Test
    public void testValidEventIsParsed() {
        PCollectionTuple result =
                pipeline.apply(Create.of(VALID_EVENT))
                        .apply("Parse", JsonToGameplayEvents.create());

        PAssert.that(result.get(JsonToGameplayEvents.SUCCESS_TAG))
                .satisfies(
                        events -> {
                            GameplayEvent event = events.iterator().next();
                            assertEquals("player_0001", event.getPlayerId());
                            assertEquals("session_1", event.getSessionId());
                            assertEquals("level_failed", event.getEventType());
                            assertEquals(Integer.valueOf(12), event.getLevel());
                            assertEquals(Long.valueOf(9100L), event.getScore());
                            assertEquals("2026-09-11T09:53:50.000Z", event.getEventTimestamp());
                            return null;
                        });
        PAssert.that(result.get(JsonToGameplayEvents.ERROR_TAG)).empty();

        pipeline.run();
    }

    @Test
    public void testMalformedJsonIsDeadLettered() {
        String malformed = "{\"player_id\": \"player_1\", \"level\": }";

        PCollectionTuple result =
                pipeline.apply(Create.of(malformed)).apply("Parse", JsonToGameplayEvents.create());

        PAssert.that(result.get(JsonToGameplayEvents.SUCCESS_TAG)).empty();
        PAssert.that(result.get(JsonToGameplayEvents.ERROR_TAG))
                .satisfies(
                        errors -> {
                            ProcessingError error = errors.iterator().next();
                            assertEquals(JsonToGameplayEvents.PARSE_STAGE, error.getStage());
                            assertEquals(malformed, error.getPayload());
                            assertTrue(!error.getErrorMessage().isEmpty());
                            return null;
                        });

        pipeline.run();
    }

    @Test
    public void testNonJsonPayloadIsDeadLettered() {
        PCollectionTuple result =
                pipeline.apply(Create.of("RAW_UNPARSEABLE_GAMEPLAY_EVENT"))
                        .apply("Parse", JsonToGameplayEvents.create());

        PAssert.that(result.get(JsonToGameplayEvents.SUCCESS_TAG)).empty();
        PAssert.that(result.get(JsonToGameplayEvents.ERROR_TAG))
                .satisfies(
                        errors -> {
                            assertEquals(
                                    JsonToGameplayEvents.PARSE_STAGE,
                                    errors.iterator().next().getStage());
                            return null;
                        });

        pipeline.run();
    }

    @Test
    public void testMissingPlayerIdIsDeadLettered() {
        String noPlayer =
                "{\"session_id\": \"session_1\", \"event_type\": \"level_failed\","
                        + " \"event_timestamp\": \"2026-09-11T09:53:50.000Z\"}";

        PCollectionTuple result =
                pipeline.apply(Create.of(noPlayer)).apply("Parse", JsonToGameplayEvents.create());

        PAssert.that(result.get(JsonToGameplayEvents.SUCCESS_TAG)).empty();
        PAssert.that(result.get(JsonToGameplayEvents.ERROR_TAG))
                .satisfies(
                        errors -> {
                            ProcessingError error = errors.iterator().next();
                            assertEquals(JsonToGameplayEvents.VALIDATE_STAGE, error.getStage());
                            assertTrue(error.getErrorMessage().contains("player_id"));
                            return null;
                        });

        pipeline.run();
    }

    @Test
    public void testUnparseableEventTimestampIsDeadLettered() {
        String badTimestamp =
                "{\"player_id\": \"player_1\", \"event_timestamp\": \"last tuesday\"}";

        PCollectionTuple result =
                pipeline.apply(Create.of(badTimestamp))
                        .apply("Parse", JsonToGameplayEvents.create());

        PAssert.that(result.get(JsonToGameplayEvents.SUCCESS_TAG)).empty();
        PAssert.that(result.get(JsonToGameplayEvents.ERROR_TAG))
                .satisfies(
                        errors -> {
                            ProcessingError error = errors.iterator().next();
                            assertEquals(JsonToGameplayEvents.VALIDATE_STAGE, error.getStage());
                            assertTrue(error.getErrorMessage().contains("event_timestamp"));
                            return null;
                        });

        pipeline.run();
    }

    @Test
    public void testMissingEventTimestampIsBackfilled() {
        // event_timestamp is REQUIRED in BigQuery: a payload without one must still be usable.
        String noTimestamp = "{\"player_id\": \"player_1\", \"event_type\": \"level_start\"}";

        PCollectionTuple result =
                pipeline.apply(Create.of(noTimestamp))
                        .apply("Parse", JsonToGameplayEvents.create());

        PAssert.that(result.get(JsonToGameplayEvents.SUCCESS_TAG))
                .satisfies(
                        events -> {
                            GameplayEvent event = events.iterator().next();
                            assertTrue(event.getEventTimestamp() != null);
                            // Must be a valid instant, otherwise BigQuery would reject the row.
                            Instant.parse(event.getEventTimestamp());
                            return null;
                        });

        pipeline.run();
    }

    @Test
    public void testValidAndInvalidPayloadsAreSeparated() {
        PCollectionTuple result =
                pipeline.apply(Create.of(Arrays.asList(VALID_EVENT, "NOT_JSON", "{\"level\": 3}")))
                        .apply("Parse", JsonToGameplayEvents.create());

        PAssert.that(result.get(JsonToGameplayEvents.SUCCESS_TAG))
                .satisfies(
                        events -> {
                            assertEquals(1, count(events));
                            return null;
                        });
        PAssert.that(result.get(JsonToGameplayEvents.ERROR_TAG))
                .satisfies(
                        errors -> {
                            assertEquals(2, count(errors));
                            return null;
                        });

        pipeline.run();
    }

    private static int count(Iterable<?> elements) {
        int size = 0;
        for (Object unused : elements) {
            size++;
        }
        return size;
    }
}
