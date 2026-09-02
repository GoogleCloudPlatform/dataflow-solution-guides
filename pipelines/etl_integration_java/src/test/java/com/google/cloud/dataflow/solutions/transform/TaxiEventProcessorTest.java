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

package com.google.cloud.dataflow.solutions.transform;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;

import com.google.cloud.dataflow.solutions.data.TaxiObjects.TaxiEvent;
import java.io.Serializable;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import org.apache.beam.sdk.io.gcp.pubsub.PubsubMessage;
import org.apache.beam.sdk.testing.PAssert;
import org.apache.beam.sdk.testing.TestPipeline;
import org.apache.beam.sdk.transforms.Create;
import org.apache.beam.sdk.values.PCollection;
import org.junit.Rule;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.JUnit4;

@RunWith(JUnit4.class)
public class TaxiEventProcessorTest implements Serializable {

    @Rule public final transient TestPipeline pipeline = TestPipeline.create();

    @Test
    public void testParseValidPubsubMessage() {
        String json =
                """
                {
                  "ride_id": "ride-123",
                  "point_idx": 5,
                  "latitude": 40.7128,
                  "longitude": -74.0060,
                  "timestamp": "2026-09-01T12:00:00Z",
                  "meter_reading": 12.5,
                  "meter_increment": 0.05,
                  "ride_status": "enroute",
                  "passenger_count": 2
                }
                """;

        PubsubMessage message =
                new PubsubMessage(json.getBytes(StandardCharsets.UTF_8), Collections.emptyMap());

        PCollection<PubsubMessage> input = pipeline.apply(Create.of(message));
        PCollection<TaxiEvent> parsedEvents =
                input.apply("Parse", TaxiEventProcessor.FromPubsubMessage.parse());

        PAssert.that(parsedEvents)
                .satisfies(
                        events -> {
                            TaxiEvent event = events.iterator().next();
                            assertNotNull(event);
                            assertEquals("ride-123", event.getRideId());
                            assertEquals(Integer.valueOf(5), event.getPointIdx());
                            assertEquals(Double.valueOf(40.7128), event.getLatitude());
                            assertEquals(Double.valueOf(-74.0060), event.getLongitude());
                            assertEquals("2026-09-01T12:00:00Z", event.getTimeStamp());
                            assertEquals(Double.valueOf(12.5), event.getMeterReading());
                            assertEquals(Double.valueOf(0.05), event.getMeterIncrement());
                            assertEquals("enroute", event.getRideStatus());
                            assertEquals(Integer.valueOf(2), event.getPassengerCount());
                            return null;
                        });

        pipeline.run();
    }

    @Test
    public void testMalformedJsonIgnored() {
        String malformedJson = "{\"ride_id\": \"incomplete-json";

        PubsubMessage message =
                new PubsubMessage(
                        malformedJson.getBytes(StandardCharsets.UTF_8), Collections.emptyMap());

        PCollection<PubsubMessage> input = pipeline.apply(Create.of(message));
        PCollection<TaxiEvent> parsedEvents =
                input.apply("Parse", TaxiEventProcessor.FromPubsubMessage.parse());

        PAssert.that(parsedEvents).empty();

        pipeline.run();
    }

    @Test
    public void testExtraFieldsSanitized() {
        String jsonWithExtraFields =
                """
                {
                  "ride_id": "ride-456",
                  "point_idx": 10,
                  "latitude": 41.8781,
                  "longitude": -87.6298,
                  "timestamp": "2026-09-01T13:00:00Z",
                  "meter_reading": 25.0,
                  "meter_increment": 0.10,
                  "ride_status": "pickup",
                  "passenger_count": 1,
                  "unexpected_extra": "ignored_value"
                }
                """;

        PubsubMessage message =
                new PubsubMessage(
                        jsonWithExtraFields.getBytes(StandardCharsets.UTF_8),
                        Collections.emptyMap());

        PCollection<PubsubMessage> input = pipeline.apply(Create.of(message));
        PCollection<TaxiEvent> parsedEvents =
                input.apply("Parse", TaxiEventProcessor.FromPubsubMessage.parse());

        PAssert.that(parsedEvents)
                .satisfies(
                        events -> {
                            TaxiEvent event = events.iterator().next();
                            assertNotNull(event);
                            assertEquals("ride-456", event.getRideId());
                            assertEquals(Integer.valueOf(10), event.getPointIdx());
                            return null;
                        });

        pipeline.run();
    }
}
