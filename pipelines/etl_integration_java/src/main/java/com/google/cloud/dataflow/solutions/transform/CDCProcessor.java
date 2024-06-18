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

import static com.google.cloud.dataflow.solutions.data.TaxiObjects.CDCValue;

import com.google.api.services.bigquery.model.TableRow;
import com.google.cloud.dataflow.solutions.data.SchemaUtils;
import com.google.cloud.dataflow.solutions.data.TaxiObjects.MergedCDCValue;
import com.google.cloud.dataflow.solutions.data.TaxiObjects.TaxiEvent;
import com.google.cloud.dataflow.solutions.transform.TaxiEventProcessor.ParsingOutput;
import java.util.Objects;
import org.apache.beam.sdk.io.gcp.bigquery.BigQueryUtils;
import org.apache.beam.sdk.io.gcp.bigquery.RowMutationInformation;
import org.apache.beam.sdk.io.gcp.spanner.changestreams.model.DataChangeRecord;
import org.apache.beam.sdk.io.gcp.spanner.changestreams.model.Mod;
import org.apache.beam.sdk.io.gcp.spanner.changestreams.model.ModType;
import org.apache.beam.sdk.io.gcp.spanner.changestreams.model.ValueCaptureType;
import org.apache.beam.sdk.schemas.Schema;
import org.apache.beam.sdk.schemas.SchemaRegistry;
import org.apache.beam.sdk.schemas.utils.ConvertHelpers;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.transforms.SerializableFunction;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.Row;
import org.apache.beam.sdk.values.TypeDescriptor;

public class CDCProcessor {

    public static class ParseCDCRecord
            extends PTransform<PCollection<DataChangeRecord>, ParsingOutput<MergedCDCValue>> {
        public static ParseCDCRecord create() {
            return new ParseCDCRecord();
        }

        @Override
        public ParsingOutput<MergedCDCValue> expand(PCollection<DataChangeRecord> input) {
            PCollection<String> jsons = input.apply("ToJson", ParDo.of(new RecordToJsonDoFn()));

            Schema cdcSchema = SchemaUtils.getSchemaForType(input.getPipeline(), CDCValue.class);

            ParsingOutput<CDCValue> parsed =
                    jsons.apply(
                            "Parse from Json String",
                            TaxiEventProcessor.FromJsonString.<CDCValue>builder()
                                    .schema(cdcSchema)
                                    .clz(CDCValue.class)
                                    .build());

            SchemaRegistry registry = input.getPipeline().getSchemaRegistry();
            Schema schemaForTaxi = SchemaUtils.getSchemaForType(input.getPipeline(), TaxiEvent.class);
            ConvertHelpers.ConvertedSchemaInformation<TaxiEvent> converted =
                    ConvertHelpers.getConvertedSchemaInformation(
                            schemaForTaxi, TypeDescriptor.of(TaxiEvent.class), registry);

            SerializableFunction<TaxiEvent, Row> toRowFunction =
                    converted.outputSchemaCoder.getToRowFunction();

            PCollection<MergedCDCValue> mergedCdc =
                    parsed.getParsedData()
                            .apply("Merge CDC", ParDo.of(new MergeCDCDoFn(toRowFunction)));

            return new ParsingOutput<>(input.getPipeline(), mergedCdc, parsed.getErrors());
        }
    }

    public static RowMutationInformation rowMutationInformation(MergedCDCValue cdc) {
        Long sequenceNumber = 0L;
        if (cdc != null) {
            sequenceNumber = cdc.getSequenceNumber();
        }

        RowMutationInformation.MutationType mutationType =
                RowMutationInformation.MutationType.UPSERT;
        if (Objects.requireNonNull(cdc.getModType()) == ModType.DELETE) {
            mutationType = RowMutationInformation.MutationType.DELETE;
        }

        return RowMutationInformation.of(mutationType, sequenceNumber);
    }

    private static class RecordToJsonDoFn extends DoFn<DataChangeRecord, String> {
        @ProcessElement
        public void processElement(
                @Element DataChangeRecord record, OutputReceiver<String> receiver) {
            ValueCaptureType valueCaptureType = record.getValueCaptureType();
            if (valueCaptureType != ValueCaptureType.NEW_ROW_AND_OLD_VALUES) {
                throw new IllegalArgumentException(
                        "This pipeline works only with capture type NEW_ROW_AND_OLD_VALUES, but"
                                + " capture type is : "
                                + valueCaptureType);
            }

            Long sequenceNumber = Long.valueOf(record.getRecordSequence());

            for (Mod mod : record.getMods()) {
                switch (record.getModType()) {
                    case INSERT -> {
                        receiver.output(
                                formatJson(
                                        mod.getNewValuesJson(),
                                        record.getModType(),
                                        sequenceNumber));
                    }
                    case DELETE -> {
                        receiver.output(
                                formatJson(mod.getKeysJson(), record.getModType(), sequenceNumber));
                    }
                    case UPDATE -> {
                        receiver.output(
                                formatJson(
                                        mod.getOldValuesJson(),
                                        mod.getNewValuesJson(),
                                        record.getModType(),
                                        sequenceNumber));
                    }
                    case UNKNOWN -> throw new IllegalArgumentException(
                            "UNKNOWN mod type, not supported");
                }
            }
        }

        private static String formatJson(String newValue, ModType modType, Long sequenceNumber) {
            return formatJson("", newValue, modType, sequenceNumber);
        }

        private static String formatJson(
                String oldValue, String newValue, ModType modType, Long sequenceNumber) {
            if (oldValue == null || oldValue.isBlank()) oldValue = "\"\"";
            if (newValue == null || newValue.isBlank()) newValue = "\"\"";

            return String.format(
                    "{\"new_event\": %s, \"old_event\": %s, \"mod_type\": \"%s\","
                            + " \"sequence_number\": %d}",
                    newValue, oldValue, modType.toString(), sequenceNumber);
        }
    }

    private static class MergeCDCDoFn extends DoFn<CDCValue, MergedCDCValue> {
        private final SerializableFunction<TaxiEvent, Row> taxiToRowFunction;

        public MergeCDCDoFn(SerializableFunction<TaxiEvent, Row> taxiToRowFunction) {
            this.taxiToRowFunction = taxiToRowFunction;
        }

        @ProcessElement
        public void processElement(@Element CDCValue cdc, OutputReceiver<MergedCDCValue> receiver) {
            switch (cdc.getModType()) {
                case INSERT, DELETE, UPDATE -> {
                    Row taxiRow = taxiToRowFunction.apply(cdc.getNewEvent());
                    TableRow tr = BigQueryUtils.toTableRow(taxiRow);

                    receiver.output(
                            MergedCDCValue.builder()
                                    .setTableRow(tr)
                                    .setModType(cdc.getModType())
                                    .setSequenceNumber(cdc.getSequenceNumber())
                                    .build());
                }
                case UNKNOWN -> throw new IllegalArgumentException(
                        "UNKNOWN mod type, not supported");
            }
        }
    }
}
