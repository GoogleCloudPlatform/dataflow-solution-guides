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
import com.google.common.collect.ImmutableMap;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import org.apache.beam.sdk.Pipeline;
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
import org.apache.beam.sdk.values.PInput;
import org.apache.beam.sdk.values.POutput;
import org.apache.beam.sdk.values.PValue;
import org.apache.beam.sdk.values.Row;
import org.apache.beam.sdk.values.TupleTag;

public class TaxiEventProcessor {

    @AutoValue
    public abstract static class FromPubsubMessage
            extends PTransform<PCollection<PubsubMessage>, ParsingOutput<TaxiObjects.TaxiEvent>> {

        public static FromPubsubMessage parse() {
            return new AutoValue_TaxiEventProcessor_FromPubsubMessage();
        }

        @Override
        public ParsingOutput<TaxiObjects.TaxiEvent> expand(PCollection<PubsubMessage> rides) {

            PCollection<String> ridesAsStrings =
                    rides.apply("Convert to Json String", ParDo.of(new ExtractPayloadDoFn()));

            Schema taxiEventSchema =
                    SchemaUtils.getSchemaForType(rides.getPipeline(), TaxiObjects.TaxiEvent.class);

            return ridesAsStrings.apply(
                    "Parse from Json String",
                    FromJsonString.<TaxiObjects.TaxiEvent>builder()
                            .schema(taxiEventSchema)
                            .clz(TaxiObjects.TaxiEvent.class)
                            .build());
        }
    }

    @AutoValue
    public abstract static class FromJsonString<T>
            extends PTransform<PCollection<String>, ParsingOutput<T>> {

        public abstract Schema schema();

        public abstract Class<T> clz();

        public static <T> Builder<T> builder() {
            return new AutoValue_TaxiEventProcessor_FromJsonString.Builder<T>();
        }

        @AutoValue.Builder
        public abstract static class Builder<T> {
            public abstract Builder<T> schema(Schema schema);

            public abstract Builder<T> clz(Class<T> clz);

            public abstract FromJsonString<T> build();
        }

        @Override
        public ParsingOutput<T> expand(PCollection<String> input) {
            PCollectionRowTuple allRows = input.apply("Convert to Row", new JsonStringParser());

            PCollection<Row> rows = allRows.get(JsonStringParser.RESULTS_TAG);
            PCollection<Row> errorRows = allRows.get(JsonStringParser.ERRORS_TAG);
            PCollection<TaxiObjects.ParsingError> errorMessages =
                    errorRows.apply("Convert to ErrorMessage", new RowToError());

            // Convert row objects to TaxiRide objects
            PCollection<T> taxiRides = rows.apply("Convert to TaxiRides", Convert.fromRows(clz()));

            return new ParsingOutput<>(input.getPipeline(), taxiRides, errorMessages);
        }
    }

    public static class ParsingOutput<T> implements POutput {

        private final Pipeline p;
        private final PCollection<T> parsed;
        private final PCollection<TaxiObjects.ParsingError> errors;
        private final TupleTag<T> parsedTag = new TupleTag<>() {};
        private final TupleTag<TaxiObjects.ParsingError> errorsTag = new TupleTag<>() {};

        public ParsingOutput(
                Pipeline p, PCollection<T> parsed, PCollection<TaxiObjects.ParsingError> errors) {
            this.p = p;
            this.parsed = parsed;
            this.errors = errors;
        }

        @Override
        public Pipeline getPipeline() {
            return p;
        }

        @Override
        public Map<TupleTag<?>, PValue> expand() {
            return ImmutableMap.of(parsedTag, parsed, errorsTag, errors);
        }

        @Override
        public void finishSpecifyingOutput(
                String transformName, PInput input, PTransform<?, ?> transform) {}

        public PCollection<T> getParsedData() {
            return parsed;
        }

        public PCollection<TaxiObjects.ParsingError> getErrors() {
            return errors;
        }
    }

    private static class JsonStringParser
            extends PTransform<PCollection<String>, PCollectionRowTuple> {

        public static final String RESULTS_TAG = "RESULTS_TAG";
        public static final String ERRORS_TAG = "ERRORS_TAG";

        @Override
        public PCollectionRowTuple expand(PCollection<String> input) {

            Schema taxiEventSchema =
                    SchemaUtils.getSchemaForType(input.getPipeline(), TaxiObjects.TaxiEvent.class);
            JsonToRow.ParseResult parseResult =
                    input.apply(
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
