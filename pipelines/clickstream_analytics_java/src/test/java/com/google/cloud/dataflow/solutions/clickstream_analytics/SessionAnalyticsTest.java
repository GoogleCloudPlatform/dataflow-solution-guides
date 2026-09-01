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
package com.google.cloud.dataflow.solutions.clickstream_analytics;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import com.google.cloud.dataflow.solutions.clickstream_analytics.data.ClickstreamObjects.ClickstreamEvent;
import com.google.cloud.dataflow.solutions.clickstream_analytics.data.ClickstreamObjects.UserSession;
import java.io.Serializable;
import java.util.Arrays;
import java.util.List;
import org.apache.beam.sdk.testing.PAssert;
import org.apache.beam.sdk.testing.TestPipeline;
import org.apache.beam.sdk.transforms.Create;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.TimestampedValue;
import org.joda.time.Instant;
import org.junit.Rule;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.JUnit4;

@RunWith(JUnit4.class)
public class SessionAnalyticsTest implements Serializable {

    @Rule public final transient TestPipeline pipeline = TestPipeline.create();

    @Test
    public void testSessionWindowAggregation() {
        Instant baseTime = Instant.parse("2026-09-01T12:00:00Z");

        ClickstreamEvent event1 =
                ClickstreamEvent.builder()
                        .setUserId("user_alpha")
                        .setPrev("Home")
                        .setCurr("Products")
                        .setType("link")
                        .setN(1)
                        .build();

        ClickstreamEvent event2 =
                ClickstreamEvent.builder()
                        .setUserId("user_alpha")
                        .setPrev("Products")
                        .setCurr("Cart")
                        .setType("link")
                        .setN(1)
                        .build();

        List<TimestampedValue<ClickstreamEvent>> events =
                Arrays.asList(
                        TimestampedValue.of(event1, baseTime),
                        TimestampedValue.of(
                                event2, baseTime.plus(org.joda.time.Duration.standardMinutes(5))));

        PCollection<ClickstreamEvent> input = pipeline.apply(Create.timestamped(events));

        // Inactivity gap: 10 minutes -> event1 and event2 (5 min apart) merge into 1 session
        PCollection<UserSession> sessions = input.apply(SessionAnalytics.of(10));

        PAssert.that(sessions)
                .satisfies(
                        userSessions -> {
                            UserSession session = userSessions.iterator().next();
                            assertEquals("user_alpha", session.getUserId());
                            assertNotNull(session.getSessionId());
                            assertTrue(session.getSessionId().startsWith("user_alpha_"));
                            assertEquals(Integer.valueOf(2), session.getEventCount());
                            assertEquals(Integer.valueOf(2), session.getUniquePagesCount());
                            assertEquals("Products", session.getFirstPage());
                            assertEquals("Cart", session.getLastPage());
                            assertEquals(Integer.valueOf(2), session.getTotalViews());
                            return null;
                        });

        pipeline.run();
    }
}
