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
package com.google.cloud.dataflow.solutions.gaming_analytics.inference;

import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.EnrichedEvent;
import java.io.Serializable;

/**
 * Scores an enriched gameplay event.
 *
 * <p>Implementations are held by a {@code DoFn}, so they must be serializable; any non-serializable
 * client is created in {@link #setUp()} and released in {@link #tearDown()}.
 */
public interface Recommender extends Serializable {

    /** Called once per {@code DoFn} instance, before the first element is scored. */
    default void setUp() throws Exception {}

    /**
     * Scores a single event.
     *
     * @param event the event, hydrated with the player features
     * @return the recommendation to activate
     * @throws Exception if the event cannot be scored; the element is then dead-lettered
     */
    Prediction predict(EnrichedEvent event) throws Exception;

    /** Called once per {@code DoFn} instance, on a best-effort basis, at the end of its life. */
    default void tearDown() {}
}
