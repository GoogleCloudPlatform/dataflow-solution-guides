/*
 * Copyright 2024 Google.
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

import com.google.api.services.bigquery.model.TableRow;
import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import org.apache.beam.sdk.coders.Coder.Context;
import org.apache.beam.sdk.io.gcp.bigquery.TableRowJsonCoder;
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

public class JsonToTableRows {

    private static int MESSAGE_LIMIT_SIZE = 10 * 1024 * 1024;

    public static PTransform<PCollection<String>, PCollectionTuple> run() {
        return new JsonToTableRows.JsonToTableRow();
    }

    static final TupleTag<TableRow> SUCCESS_TAG = new TupleTag<TableRow>() {};
    static final TupleTag<KV<String, String>> FAILURE_TAG = new TupleTag<KV<String, String>>() {};

    private static class JsonToTableRow extends PTransform<PCollection<String>, PCollectionTuple> {

        @Override
        public PCollectionTuple expand(PCollection<String> jsonStrings) {
            return jsonStrings.apply(
                    ParDo.of(new ToJsonDoFn())
                            .withOutputTags(SUCCESS_TAG, TupleTagList.of(FAILURE_TAG)));
        }
    }

    private static class ToJsonDoFn extends DoFn<String, TableRow> {
        public static final Logger LOG = LoggerFactory.getLogger(ToJsonDoFn.class);

        @ProcessElement
        public void processElement(ProcessContext context) {
            String jsonString = context.element();

            byte[] message_in_bytes = jsonString.getBytes(StandardCharsets.UTF_8);

            if (message_in_bytes.length >= JsonToTableRows.MESSAGE_LIMIT_SIZE) {
                LOG.error("Row is too big row, size {} bytes", message_in_bytes.length);
                Metrics.tooBigMessages.inc();
                context.output(FAILURE_TAG, KV.of("TooBigRow", jsonString));
            }

            TableRow row;
            try (InputStream inputStream = new ByteArrayInputStream(message_in_bytes)) {
                row = TableRowJsonCoder.of().decode(inputStream, Context.OUTER);
                Metrics.successfulMessages.inc();
                context.output(row);

            } catch (IOException e) {
                LOG.error(e.getMessage());
                Metrics.jsonParseErrorMessages.inc();
                context.output(FAILURE_TAG, KV.of("JsonParseError", jsonString));
            }
        }
    }
}
