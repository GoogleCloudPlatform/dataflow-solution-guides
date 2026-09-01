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
package com.google.cloud.dataflow.solutions.clickstream_analytics.extract;

import static org.junit.Assert.assertEquals;

import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.JUnit4;

@RunWith(JUnit4.class)
public class PubSubTest {

    @Test
    public void testPubSubReadBuilder() {
        String subscription = "projects/test-project/subscriptions/test-sub";
        PubSub.Read readTransform = PubSub.read().withSubscription(subscription).build();

        assertEquals(subscription, readTransform.subscription());
    }

    @Test
    public void testPubSubFromSubscription() {
        String subscription = "projects/test-project/subscriptions/test-sub-2";
        PubSub.Read readTransform = PubSub.fromSubscription(subscription);

        assertEquals(subscription, readTransform.subscription());
    }
}
