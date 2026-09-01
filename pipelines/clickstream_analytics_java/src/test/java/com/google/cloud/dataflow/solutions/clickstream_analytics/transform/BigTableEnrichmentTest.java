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
import static org.junit.Assert.assertNull;

import com.google.cloud.dataflow.solutions.clickstream_analytics.data.ClickstreamObjects.ClickstreamEvent;
import java.io.Serializable;
import org.apache.beam.sdk.testing.PAssert;
import org.apache.beam.sdk.testing.TestPipeline;
import org.apache.beam.sdk.transforms.Create;
import org.apache.beam.sdk.values.PCollection;
import org.junit.Rule;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.JUnit4;

@RunWith(JUnit4.class)
public class BigTableEnrichmentTest implements Serializable {

    @Rule public final transient TestPipeline pipeline = TestPipeline.create();

    @Test
    public void testResolveRowKey() {
        ClickstreamEvent event =
                ClickstreamEvent.builder()
                        .setUserId("user_42")
                        .setPrev("Search")
                        .setCurr("BigQuery")
                        .setType("link")
                        .setN(1)
                        .build();

        assertEquals("BigQuery", BigTableEnrichment.resolveRowKey(event, "curr"));
        assertEquals("Search", BigTableEnrichment.resolveRowKey(event, "prev"));
        assertEquals("user_42", BigTableEnrichment.resolveRowKey(event, "user_id"));
        assertNull(BigTableEnrichment.resolveRowKey(null, "curr"));
    }

    @Test
    public void testDisabledEnrichmentPassesThrough() {
        ClickstreamEvent event =
                ClickstreamEvent.builder()
                        .setUserId("user_1")
                        .setCurr("Dataflow")
                        .setPrev("Main_Page")
                        .setType("link")
                        .setN(1)
                        .build();

        PCollection<ClickstreamEvent> input = pipeline.apply(Create.of(event));

        PCollection<ClickstreamEvent> enriched =
                input.apply(
                        "DisabledBigtableEnrichment",
                        BigTableEnrichment.create()
                                .withProjectId("test-proj")
                                .withInstanceId("test-inst")
                                .withTableId("test-table")
                                .withLookupKeyField("curr")
                                .withEnabled(false));

        PAssert.that(enriched)
                .satisfies(
                        events -> {
                            ClickstreamEvent out = events.iterator().next();
                            assertEquals("Dataflow", out.getCurr());
                            assertNull(out.getCategory());
                            assertNull(out.getEnrichedData());
                            return null;
                        });

        pipeline.run();
    }
}
