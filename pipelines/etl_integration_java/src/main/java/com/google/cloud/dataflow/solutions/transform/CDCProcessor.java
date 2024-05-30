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

import org.apache.beam.sdk.io.gcp.spanner.changestreams.model.ChangeStreamRecordMetadata;
import org.apache.beam.sdk.io.gcp.spanner.changestreams.model.DataChangeRecord;
import org.apache.beam.sdk.io.gcp.spanner.changestreams.model.Mod;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.values.Row;

public class CDCProcessor {

    private static class DataChangeRecordDoFn extends DoFn<DataChangeRecord, Row> {
        @ProcessElement
        public void processElement(@Element DataChangeRecord record, OutputReceiver<Row> receiver) {
            Mod mod = record.getMods().get(0);
            switch (record.getModType()) {
                case INSERT -> {}
                case DELETE -> {}
                case UPDATE -> {}
                case UNKNOWN -> {}
            }
        }
    }
}
