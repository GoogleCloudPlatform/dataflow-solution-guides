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

import com.google.cloud.dataflow.solutions.clickstream_analytics.data.ClickstreamObjects.ClickstreamEvent;
import com.google.cloud.dataflow.solutions.clickstream_analytics.data.ClickstreamObjects.UserSession;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.GroupByKey;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.transforms.windowing.IntervalWindow;
import org.apache.beam.sdk.transforms.windowing.Sessions;
import org.apache.beam.sdk.transforms.windowing.Window;
import org.apache.beam.sdk.values.KV;
import org.apache.beam.sdk.values.PCollection;
import org.joda.time.Duration;
import org.joda.time.Instant;

public class SessionAnalytics
        extends PTransform<PCollection<ClickstreamEvent>, PCollection<UserSession>> {

    private final int gapDurationMinutes;

    public SessionAnalytics(int gapDurationMinutes) {
        this.gapDurationMinutes = gapDurationMinutes > 0 ? gapDurationMinutes : 30;
    }

    public static SessionAnalytics of(int gapDurationMinutes) {
        return new SessionAnalytics(gapDurationMinutes);
    }

    @Override
    public PCollection<UserSession> expand(PCollection<ClickstreamEvent> events) {
        return events.apply(
                        "KeyByUser",
                        ParDo.of(
                                new DoFn<ClickstreamEvent, KV<String, ClickstreamEvent>>() {
                                    @ProcessElement
                                    public void processElement(ProcessContext c) {
                                        ClickstreamEvent event = c.element();
                                        String key = event.getUserId();
                                        if (key == null || key.trim().isEmpty()) {
                                            key = "anonymous";
                                        }
                                        if (event.getTimestamp() == null && c.timestamp() != null) {
                                            event =
                                                    event.toBuilder()
                                                            .setTimestamp(c.timestamp().toString())
                                                            .build();
                                        }
                                        c.output(KV.of(key, event));
                                    }
                                }))
                .apply(
                        "ApplySessionWindow",
                        Window.<KV<String, ClickstreamEvent>>into(
                                Sessions.withGapDuration(
                                        Duration.standardMinutes(gapDurationMinutes))))
                .apply("GroupSessionsByKey", GroupByKey.create())
                .apply("AggregateSessionMetrics", ParDo.of(new AggregateSessionDoFn()));
    }

    public static class AggregateSessionDoFn
            extends DoFn<KV<String, Iterable<ClickstreamEvent>>, UserSession> {

        private static Instant parseTimestamp(String ts) {
            if (ts == null || ts.trim().isEmpty()) {
                return Instant.EPOCH;
            }
            try {
                return Instant.parse(ts);
            } catch (Exception e) {
                try {
                    return Instant.ofEpochMilli(Long.parseLong(ts));
                } catch (Exception ex) {
                    return Instant.EPOCH;
                }
            }
        }

        @ProcessElement
        public void processElement(
                @Element KV<String, Iterable<ClickstreamEvent>> element,
                IntervalWindow window,
                OutputReceiver<UserSession> receiver) {

            String userId = element.getKey();
            Iterable<ClickstreamEvent> events = element.getValue();

            List<ClickstreamEvent> sortedEvents = new ArrayList<>();
            for (ClickstreamEvent event : events) {
                sortedEvents.add(event);
            }
            sortedEvents.sort(Comparator.comparing(e -> parseTimestamp(e.getTimestamp())));

            int eventCount = 0;
            int totalViews = 0;
            Set<String> uniquePages = new HashSet<>();
            String firstPage = null;
            String lastPage = null;

            for (ClickstreamEvent event : sortedEvents) {
                eventCount++;
                totalViews += (event.getN() != null ? event.getN() : 1);

                String page = event.getCurr();
                if (page != null && !page.isEmpty()) {
                    uniquePages.add(page);
                    if (firstPage == null) {
                        firstPage = page;
                    }
                    lastPage = page;
                }
            }

            String sessionId = String.format("%s_%d", userId, window.start().getMillis());
            double durationSeconds =
                    (window.end().getMillis() - window.start().getMillis()) / 1000.0;

            UserSession session =
                    UserSession.builder()
                            .setSessionId(sessionId)
                            .setUserId(userId)
                            .setSessionStart(window.start().toString())
                            .setSessionEnd(window.end().toString())
                            .setDurationSeconds(durationSeconds)
                            .setEventCount(eventCount)
                            .setFirstPage(firstPage)
                            .setLastPage(lastPage)
                            .setUniquePagesCount(uniquePages.size())
                            .setTotalViews(totalViews)
                            .build();

            Metrics.sessionsProcessed.inc();
            receiver.output(session);
        }
    }
}
