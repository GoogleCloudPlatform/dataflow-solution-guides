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

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.google.auto.value.AutoValue;
import com.google.cloud.dataflow.solutions.clickstream_analytics.Metrics;
import com.google.cloud.dataflow.solutions.clickstream_analytics.data.ClickstreamObjects.ClickstreamEvent;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.KV;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PCollectionTuple;
import org.apache.beam.sdk.values.TupleTag;
import org.apache.beam.sdk.values.TupleTagList;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

@AutoValue
public abstract class JsonToEvents extends PTransform<PCollection<String>, PCollectionTuple> {

    public static final int DEFAULT_MESSAGE_LIMIT_SIZE = 10 * 1024 * 1024;
    public static final int MESSAGE_LIMIT_SIZE = DEFAULT_MESSAGE_LIMIT_SIZE;

    public static final TupleTag<ClickstreamEvent> SUCCESS_TAG =
            new TupleTag<ClickstreamEvent>() {};
    public static final TupleTag<KV<String, String>> FAILURE_TAG =
            new TupleTag<KV<String, String>>() {};

    public abstract int messageLimitSize();

    public static JsonToEvents create() {
        return builder().build();
    }

    public static JsonToEvents run() {
        return create();
    }

    public static Builder builder() {
        return new AutoValue_JsonToEvents.Builder().messageLimitSize(DEFAULT_MESSAGE_LIMIT_SIZE);
    }

    public JsonToEvents withMessageLimitSize(int messageLimitSize) {
        return toBuilder().messageLimitSize(messageLimitSize).build();
    }

    public abstract Builder toBuilder();

    @AutoValue.Builder
    public abstract static class Builder {
        public abstract Builder messageLimitSize(int messageLimitSize);

        public Builder withMessageLimitSize(int messageLimitSize) {
            return messageLimitSize(messageLimitSize);
        }

        public abstract JsonToEvents build();
    }

    @Override
    public PCollectionTuple expand(PCollection<String> jsonStrings) {
        return jsonStrings.apply(
                "ParseClickstreamJson",
                ParDo.of(new ParseJsonDoFn(messageLimitSize()))
                        .withOutputTags(SUCCESS_TAG, TupleTagList.of(FAILURE_TAG)));
    }

    public static class ParseJsonDoFn extends DoFn<String, ClickstreamEvent> {
        private static final Logger LOG = LoggerFactory.getLogger(ParseJsonDoFn.class);
        private final int messageLimitSize;
        private transient ObjectMapper objectMapper;

        public ParseJsonDoFn(int messageLimitSize) {
            this.messageLimitSize = messageLimitSize;
        }

        public ParseJsonDoFn() {
            this(DEFAULT_MESSAGE_LIMIT_SIZE);
        }

        @Setup
        public void setup() {
            objectMapper = new ObjectMapper();
        }

        private ObjectMapper getMapper() {
            if (objectMapper == null) {
                objectMapper = new ObjectMapper();
            }
            return objectMapper;
        }

        @ProcessElement
        public void processElement(ProcessContext context) {
            String jsonString = context.element();
            byte[] messageBytes = jsonString.getBytes(StandardCharsets.UTF_8);

            if (messageBytes.length >= messageLimitSize) {
                LOG.error("Row is too big, size {} bytes", messageBytes.length);
                Metrics.tooBigMessages.inc();
                context.output(FAILURE_TAG, KV.of("TooBigRow", jsonString));
                return;
            }

            try {
                JsonNode node = getMapper().readTree(jsonString);
                ClickstreamEvent.Builder eventBuilder = ClickstreamEvent.builder();

                if (node.hasNonNull("user_id")) {
                    eventBuilder.setUserId(node.get("user_id").asText());
                } else if (node.hasNonNull("client_id")) {
                    eventBuilder.setUserId(node.get("client_id").asText());
                }

                if (node.hasNonNull("timestamp")) {
                    eventBuilder.setTimestamp(node.get("timestamp").asText());
                }

                if (node.hasNonNull("prev")) {
                    eventBuilder.setPrev(node.get("prev").asText());
                }

                if (node.hasNonNull("curr")) {
                    eventBuilder.setCurr(node.get("curr").asText());
                }

                if (node.hasNonNull("type")) {
                    eventBuilder.setType(node.get("type").asText());
                }

                if (node.hasNonNull("n")) {
                    eventBuilder.setN(node.get("n").asInt());
                } else {
                    eventBuilder.setN(1);
                }

                if (node.hasNonNull("category")) {
                    eventBuilder.setCategory(node.get("category").asText());
                }

                if (node.hasNonNull("enriched_data")) {
                    eventBuilder.setEnrichedData(node.get("enriched_data").asText());
                }

                ClickstreamEvent event = eventBuilder.build();
                Metrics.successfulMessages.inc();
                context.output(event);

            } catch (IOException | IllegalArgumentException e) {
                LOG.error("Failed to parse clickstream event JSON: {}", e.getMessage());
                Metrics.jsonParseErrorMessages.inc();
                context.output(FAILURE_TAG, KV.of("JsonParseError", jsonString));
            }
        }
    }
}
