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

import com.google.cloud.dataflow.solutions.data.TaxiObjects;
import org.apache.beam.sdk.io.gcp.spanner.changestreams.model.DataChangeRecord;
import org.apache.beam.sdk.io.gcp.spanner.changestreams.model.Mod;
import org.apache.beam.sdk.io.gcp.spanner.changestreams.model.ModType;
import org.apache.beam.sdk.io.gcp.spanner.changestreams.model.ValueCaptureType;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.PCollection;

public class CDCProcessor {

    private static class ParseCDCRecord
            extends PTransform<PCollection<DataChangeRecord>, PCollection<TaxiObjects.CDCValue>> {
        @Override
        public PCollection<TaxiObjects.CDCValue> expand(PCollection<DataChangeRecord> input) {
            PCollection<String> jsons = input.apply("ToJson", ParDo.of(new RecordToJsonDoFn()));

            //            JsonToRow.withExceptionReporting().withExtendedErrorInfo()

            return null;
        }
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
            for (Mod mod : record.getMods()) {
                switch (record.getModType()) {
                    case INSERT -> {
                        receiver.output(formatJson(mod.getNewValuesJson(), record.getModType()));
                    }
                    case DELETE -> {
                        receiver.output(formatJson(mod.getKeysJson(), record.getModType()));
                    }
                    case UPDATE -> {
                        receiver.output(
                                formatJson(
                                        mod.getOldValuesJson(),
                                        mod.getNewValuesJson(),
                                        record.getModType()));
                    }
                    case UNKNOWN -> throw new IllegalArgumentException(
                            "UNKNOWN mod type, not supported");
                }
            }
        }

        private static String formatJson(String newValue, ModType modType) {
            return formatJson("", newValue, modType);
        }

        private static String formatJson(String oldValue, String newValue, ModType modType) {
            if (oldValue == null || oldValue.isBlank()) oldValue = "\"\"";
            if (newValue == null || newValue.isBlank()) newValue = "\"\"";

            return String.format(
                    "{\"new_event\": %s, \"old_event\": %s, \"mod_type\": \"%s\"}",
                    newValue, oldValue, modType.toString());
        }
    }
}
