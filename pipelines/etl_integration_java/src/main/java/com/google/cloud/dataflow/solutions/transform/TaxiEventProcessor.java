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

import com.google.auto.value.AutoValue;
import com.google.cloud.dataflow.solutions.data.SchemaUtils;
import com.google.cloud.dataflow.solutions.data.TaxiObjects;
import java.nio.charset.StandardCharsets;
import org.apache.beam.sdk.io.gcp.pubsub.PubsubMessage;
import org.apache.beam.sdk.schemas.Schema;
import org.apache.beam.sdk.schemas.transforms.Convert;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.JsonToRow;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PCollectionRowTuple;
import org.apache.beam.sdk.values.PCollectionTuple;
import org.apache.beam.sdk.values.Row;
import org.apache.beam.sdk.values.TupleTag;

public class TaxiEventProcessor {
    public static final TupleTag<TaxiObjects.TaxiEvent> PARSED_MESSAGES = new TupleTag<>();
    public static final TupleTag<TaxiObjects.ParsingError> PARSING_ERRORS = new TupleTag<>();

    @AutoValue
    public abstract static class Parser
            extends PTransform<PCollection<PubsubMessage>, PCollectionTuple> {

        public static Builder builder() {
            return new AutoValue_TaxiEventProcessor_Parser.Builder();
        }

        public static Parser create() {
            return builder().build();
        }

        @AutoValue.Builder
        public abstract static class Builder {
            public abstract Parser build();
        }

        @Override
        public PCollectionTuple expand(PCollection<PubsubMessage> rides) {
            PCollectionRowTuple allRows = rides.apply("Convert to Row", new TaxiEventJsonParser());

            PCollection<Row> rows = allRows.get(TaxiEventJsonParser.RESULTS_TAG);
            PCollection<Row> errorRows = allRows.get(TaxiEventJsonParser.ERRORS_TAG);
            PCollection<TaxiObjects.ParsingError> errorMessages =
                    errorRows.apply("Convert to ErrorMessage", new RowToError());

            // Convert row objects to TaxiRide objects
            PCollection<TaxiObjects.TaxiEvent> taxiRides =
                    rows.apply(
                            "Convert to TaxiRides", Convert.fromRows(TaxiObjects.TaxiEvent.class));

            return PCollectionTuple.of(PARSED_MESSAGES, taxiRides)
                    .and(PARSING_ERRORS, errorMessages);
        }
    }

    private static class TaxiEventJsonParser
            extends PTransform<PCollection<PubsubMessage>, PCollectionRowTuple> {

        public static final String RESULTS_TAG = "RESULTS_TAG";
        public static final String ERRORS_TAG = "ERRORS_TAG";

        @Override
        public PCollectionRowTuple expand(PCollection<PubsubMessage> input) {

            Schema taxiEventSchema =
                    SchemaUtils.getSchemaForType(input.getPipeline(), TaxiObjects.TaxiEvent.class);
            JsonToRow.ParseResult parseResult =
                    input.apply("Convert to Json String", ParDo.of(new ExtractPayloadDoFn()))
                            .apply(
                                    JsonToRow.withExceptionReporting(taxiEventSchema)
                                            .withExtendedErrorInfo());

            PCollection<Row> results = parseResult.getResults();
            PCollection<Row> errors = parseResult.getFailedToParseLines();

            return PCollectionRowTuple.of(RESULTS_TAG, results).and(ERRORS_TAG, errors);
        }
    }

    private static class ExtractPayloadDoFn extends DoFn<PubsubMessage, String> {
        @DoFn.ProcessElement
        public void processElement(@Element PubsubMessage element, OutputReceiver<String> out) {

            String json = new String(element.getPayload(), StandardCharsets.UTF_8);

            out.output(json);
        }
    }
}
