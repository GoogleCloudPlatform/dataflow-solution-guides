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
package com.google.cloud.dataflow.solutions.clickstream_analytics.transform;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import com.google.cloud.dataflow.solutions.clickstream_analytics.data.ClickstreamObjects.ClickstreamEvent;
import com.google.cloud.dataflow.solutions.clickstream_analytics.data.ClickstreamObjects.ParsingError;
import java.io.Serializable;
import org.apache.beam.sdk.testing.PAssert;
import org.apache.beam.sdk.testing.TestPipeline;
import org.apache.beam.sdk.transforms.Create;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PCollectionTuple;
import org.junit.Rule;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.JUnit4;

@RunWith(JUnit4.class)
public class JsonToEventsTest implements Serializable {

    @Rule public final transient TestPipeline pipeline = TestPipeline.create();

    @Test
    public void testParseValidClickstreamJson() {
        String validJson =
                "{\"user_id\":\"user_123\",\"prev\":\"Main_Page\",\"curr\":\"Google_Cloud\",\"type\":\"link\",\"n\":2,\"timestamp\":\"2026-09-01T12:00:00Z\"}";

        PCollection<String> input = pipeline.apply(Create.of(validJson));
        PCollectionTuple results = input.apply(JsonToEvents.parseJson());

        PCollection<ClickstreamEvent> successEvents = results.get(JsonToEvents.SUCCESS_TAG);

        PAssert.that(successEvents)
                .satisfies(
                        events -> {
                            ClickstreamEvent event = events.iterator().next();
                            assertEquals("user_123", event.getUserId());
                            assertEquals("Main_Page", event.getPrev());
                            assertEquals("Google_Cloud", event.getCurr());
                            assertEquals("link", event.getType());
                            assertEquals(Integer.valueOf(2), event.getN());
                            assertEquals("2026-09-01T12:00:00Z", event.getTimestamp());
                            return null;
                        });

        pipeline.run();
    }

    @Test
    public void testParseInvalidJsonRoutesToFailureTag() {
        String invalidJson = "{invalid_json_payload: missing_quotes}";

        PCollection<String> input = pipeline.apply(Create.of(invalidJson));
        PCollectionTuple results = input.apply(JsonToEvents.create());

        PCollection<ParsingError> failureEvents = results.get(JsonToEvents.FAILURE_TAG);

        PAssert.that(failureEvents)
                .satisfies(
                        errors -> {
                            ParsingError error = errors.iterator().next();
                            assertEquals(invalidJson, error.getInputData());
                            assertEquals(invalidJson, error.getPayloadString());
                            assertNotNull(error.getErrorMessage());
                            assertTrue(!error.getErrorMessage().isEmpty());
                            assertNotNull(error.getTimestamp());
                            return null;
                        });

        pipeline.run();
    }

    @Test
    public void testSchemaTypeMismatchRoutesToErrorTag() {
        String typeMismatchJson =
                "{\"user_id\":\"user_456\",\"n\":\"not_a_valid_integer\",\"timestamp\":\"2026-09-01T12:00:00Z\"}";

        PCollection<String> input = pipeline.apply(Create.of(typeMismatchJson));
        PCollectionTuple results = input.apply(JsonToEvents.create());

        PCollection<ParsingError> failureEvents = results.get(JsonToEvents.ERROR_TAG);

        PAssert.that(failureEvents)
                .satisfies(
                        errors -> {
                            ParsingError error = errors.iterator().next();
                            assertEquals(typeMismatchJson, error.getInputData());
                            assertNotNull(error.getErrorMessage());
                            assertTrue(!error.getErrorMessage().isEmpty());
                            assertNotNull(error.getTimestamp());
                            return null;
                        });

        pipeline.run();
    }
}
