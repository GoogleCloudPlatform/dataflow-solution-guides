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

import com.google.cloud.dataflow.solutions.data.SchemaUtils;
import com.google.cloud.dataflow.solutions.data.TaxiObjects;
import org.apache.beam.sdk.coders.SerializableCoder;
import org.apache.beam.sdk.schemas.Schema;
import org.apache.beam.sdk.schemas.transforms.Convert;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.Row;
import org.joda.time.Instant;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

class RowToError extends PTransform<PCollection<Row>, PCollection<TaxiObjects.ParsingError>> {
    public static final Logger LOG = LoggerFactory.getLogger(RowToError.class);

    @Override
    public PCollection<TaxiObjects.ParsingError> expand(PCollection<Row> errorRows) {
        // Create ErrorMessage events for incompatible schema (Failed records from JsonToRow)
        Schema errorMessageSchema =
                SchemaUtils.getSchemaForType(
                        errorRows.getPipeline(), TaxiObjects.ParsingError.class);

        return errorRows
                .apply(
                        "Error Message Events",
                        ParDo.of(new GenerateJsonToRowErrorMsgDoFn(errorMessageSchema)))
                .setCoder(SerializableCoder.of(Row.class))
                .setRowSchema(errorMessageSchema)
                .apply("Error Messages to Row", Convert.fromRows(TaxiObjects.ParsingError.class));
    }

    private static class GenerateJsonToRowErrorMsgDoFn extends DoFn<Row, Row> {
        final Schema errorMessageSchema;

        public GenerateJsonToRowErrorMsgDoFn(Schema errorMessageSchema) {
            this.errorMessageSchema = errorMessageSchema;
        }

        @ProcessElement
        public void processElement(
                @FieldAccess("line") String inputData,
                @FieldAccess("err") String errorMessage,
                @Timestamp Instant timestamp,
                OutputReceiver<Row> out) {

            out.output(
                    Row.withSchema(errorMessageSchema)
                            .withFieldValue("inputData", inputData)
                            .withFieldValue("errorMessage", errorMessage)
                            .withFieldValue("timestamp", timestamp)
                            .build());
        }
    }
}
