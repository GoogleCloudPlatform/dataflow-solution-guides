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

import com.google.auto.value.AutoValue;
import com.google.cloud.dataflow.solutions.clickstream_analytics.data.ClickstreamObjects.ClickstreamEvent;
import com.google.cloud.dataflow.solutions.clickstream_analytics.data.ClickstreamObjects.ParsingError;
import org.apache.beam.sdk.metrics.Counter;
import org.apache.beam.sdk.metrics.Metrics;
import org.apache.beam.sdk.schemas.NoSuchSchemaException;
import org.apache.beam.sdk.schemas.Schema;
import org.apache.beam.sdk.schemas.transforms.Convert;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.DoFn.FieldAccess;
import org.apache.beam.sdk.transforms.JsonToRow;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PCollectionRowTuple;
import org.apache.beam.sdk.values.PCollectionTuple;
import org.apache.beam.sdk.values.Row;
import org.apache.beam.sdk.values.TupleTag;
import org.joda.time.Instant;

/** Parse JSON strings and return {@link ClickstreamEvent} elements using Apache Beam Schemas. */
@AutoValue
public abstract class JsonToEvents extends PTransform<PCollection<String>, PCollectionTuple> {

    public static final TupleTag<ClickstreamEvent> SUCCESS_TAG =
            new TupleTag<ClickstreamEvent>("SUCCESS_TAG") {};
    public static final TupleTag<ParsingError> ERROR_TAG =
            new TupleTag<ParsingError>("ERROR_TAG") {};
    public static final TupleTag<ParsingError> FAILURE_TAG = ERROR_TAG;

    public static JsonToEvents create() {
        return new AutoValue_JsonToEvents();
    }

    public static JsonToEvents parseJson() {
        return create();
    }

    @Override
    public PCollectionTuple expand(PCollection<String> input) {
        // Parse JSON strings to Rows conforming to ClickstreamEvent schema
        PCollectionRowTuple allRows = input.apply("Json2Row", new Json2Row());
        PCollection<Row> goodRows = allRows.get(Json2Row.RESULTS_TAG);
        PCollection<Row> badRows = allRows.get(Json2Row.ERROR_TAG);

        // Convert Rows to strongly typed AutoValue data classes
        PCollection<ClickstreamEvent> events =
                goodRows.apply("Row2ClickstreamEvent", Convert.fromRows(ClickstreamEvent.class));
        PCollection<ParsingError> errors = badRows.apply("Row2Error", new Row2ErrorMessage());

        return PCollectionTuple.of(SUCCESS_TAG, events).and(ERROR_TAG, errors);
    }

    /** Parses JSON to Row and verifies that data conforms to the assumed schema. */
    private static class Json2Row extends PTransform<PCollection<String>, PCollectionRowTuple> {
        static final String RESULTS_TAG = "RESULTS_TAG";
        static final String ERROR_TAG = "ERROR_TAG";

        @Override
        public PCollectionRowTuple expand(PCollection<String> input) {
            Schema eventSchema;
            try {
                eventSchema =
                        input.getPipeline().getSchemaRegistry().getSchema(ClickstreamEvent.class);
            } catch (NoSuchSchemaException e) {
                throw new IllegalStateException(
                        String.format(
                                "No schema found for ClickstreamEvent class: %s", e.getMessage()));
            }

            JsonToRow.ParseResult parseResult =
                    input.apply(
                            "Json2Row",
                            JsonToRow.withExceptionReporting(eventSchema).withExtendedErrorInfo());

            PCollection<Row> results = parseResult.getResults();
            PCollection<Row> errors = parseResult.getFailedToParseLines();

            return PCollectionRowTuple.of(RESULTS_TAG, results).and(ERROR_TAG, errors);
        }
    }

    /** Maps failed JSON parse rows into strongly typed {@link ParsingError} elements. */
    private static class Row2ErrorMessage
            extends PTransform<PCollection<Row>, PCollection<ParsingError>> {
        @Override
        public PCollection<ParsingError> expand(PCollection<Row> input) {
            Schema errorSchema;
            try {
                errorSchema = input.getPipeline().getSchemaRegistry().getSchema(ParsingError.class);
            } catch (NoSuchSchemaException e) {
                throw new IllegalStateException(
                        String.format(
                                "No schema found for ParsingError class: %s", e.getMessage()));
            }

            PCollection<Row> rowsWithRightSchema =
                    input.apply(
                            "JsonRow2ErrorMessage",
                            ParDo.of(new JsonRow2ErrorMessageRowDoFn(errorSchema)));

            return rowsWithRightSchema
                    .setRowSchema(errorSchema)
                    .apply("Row2ErrorMessage", Convert.fromRows(ParsingError.class));
        }
    }

    /** DoFn that builds an error Row matching ParsingError schema from failed JSON parse info. */
    private static class JsonRow2ErrorMessageRowDoFn extends DoFn<Row, Row> {
        private static final Counter jsonParseErrorMessages =
                Metrics.counter(JsonToEvents.class, "json-parse-failed-messages");

        private final Schema errorRowSchema;

        JsonRow2ErrorMessageRowDoFn(Schema errorRowSchema) {
            this.errorRowSchema = errorRowSchema;
        }

        @ProcessElement
        public void processElement(
                @FieldAccess("line") String inputData,
                @FieldAccess("err") String errorMessage,
                @Timestamp Instant timestamp,
                OutputReceiver<Row> outputReceiver) {
            jsonParseErrorMessages.inc();
            Row outputRow =
                    Row.withSchema(this.errorRowSchema)
                            .withFieldValue("input_data", inputData)
                            .withFieldValue("error_message", errorMessage)
                            .withFieldValue("timestamp", timestamp)
                            .build();

            outputReceiver.output(outputRow);
        }
    }
}
