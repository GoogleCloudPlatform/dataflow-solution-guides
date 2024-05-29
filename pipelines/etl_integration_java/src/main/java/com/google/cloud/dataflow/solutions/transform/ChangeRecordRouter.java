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

import java.util.ArrayList;
import java.util.List;
import org.apache.beam.sdk.io.gcp.spanner.changestreams.model.DataChangeRecord;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PCollectionTuple;
import org.apache.beam.sdk.values.TupleTag;
import org.apache.beam.sdk.values.TupleTagList;

public class ChangeRecordRouter {

    public static class ToPubsub
            extends PTransform<PCollection<DataChangeRecord>, PCollectionTuple> {

        private List<TupleTag<DataChangeRecord>> tags = new ArrayList<>();

        private ToPubsub(int numberOfTopics) {
            for (int i = 0; i < numberOfTopics; i++) {
                tags.add(new TupleTag<DataChangeRecord>());
            }
        }

        public static ToPubsub process(int numberOfTopics) {
            if (numberOfTopics < 2) {
                throw new IllegalArgumentException("numberOfTopics must be at least 2");
            }
            return new ToPubsub(numberOfTopics);
        }

        @Override
        public PCollectionTuple expand(PCollection<DataChangeRecord> input) {

            TupleTag<DataChangeRecord> firstTag = this.tags.get(0);
            List<TupleTag<?>> restOfTags = new ArrayList<>();
            for (int i = 1; i < this.tags.size(); i++) {
                restOfTags.add(this.tags.get(i));
            }

            return input.apply(
                    "Route change records",
                    ParDo.of(new RouterDoFn(tags))
                            .withOutputTags(firstTag, TupleTagList.of(restOfTags)));
        }

        public List<TupleTag<DataChangeRecord>> getTags() {
            return tags;
        }
    }

    private static class RouterDoFn extends DoFn<DataChangeRecord, DataChangeRecord> {
        private final List<TupleTag<DataChangeRecord>> tags;
        private final int numberOfTopics;

        public RouterDoFn(List<TupleTag<DataChangeRecord>> tags) {
            this.tags = tags;
            numberOfTopics = tags.size();
        }

        @ProcessElement
        public void processElement(@Element DataChangeRecord record, MultiOutputReceiver out) {
            // Generate random number between 0 and numberOfTopics-1
            int topic = (int) (Math.random() * numberOfTopics);
            out.get(tags.get(topic)).output(record);
        }
    }
}
