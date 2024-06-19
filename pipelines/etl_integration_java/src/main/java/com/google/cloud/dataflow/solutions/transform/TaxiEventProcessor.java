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

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.google.auto.value.AutoValue;
import com.google.cloud.dataflow.solutions.data.SchemaUtils;
import com.google.cloud.dataflow.solutions.data.TaxiObjects.ParsingError;
import com.google.cloud.dataflow.solutions.data.TaxiObjects.TaxiEvent;
import com.google.common.collect.ImmutableMap;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.stream.Collectors;
import java.util.stream.Stream;
import java.util.stream.StreamSupport;
import org.apache.beam.sdk.Pipeline;
import org.apache.beam.sdk.io.gcp.pubsub.PubsubMessage;
import org.apache.beam.sdk.schemas.Schema;
import org.apache.beam.sdk.schemas.Schema.Field;
import org.apache.beam.sdk.schemas.Schema.FieldType;
import org.apache.beam.sdk.schemas.Schema.TypeName;
import org.apache.beam.sdk.schemas.transforms.Convert;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.JsonToRow;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PCollectionRowTuple;
import org.apache.beam.sdk.values.PInput;
import org.apache.beam.sdk.values.POutput;
import org.apache.beam.sdk.values.PValue;
import org.apache.beam.sdk.values.Row;
import org.apache.beam.sdk.values.TupleTag;

public class TaxiEventProcessor {

    @AutoValue
    public abstract static class FromPubsubMessage
            extends PTransform<PCollection<PubsubMessage>, ParsingOutput<TaxiEvent>> {

        public static FromPubsubMessage parse() {
            return new AutoValue_TaxiEventProcessor_FromPubsubMessage();
        }

        @Override
        public ParsingOutput<TaxiEvent> expand(PCollection<PubsubMessage> rides) {

            PCollection<String> ridesAsStrings =
                    rides.apply("Convert to Json String", ParDo.of(new ExtractPayloadDoFn()));

            Schema taxiEventSchema =
                    SchemaUtils.getSchemaForType(rides.getPipeline(), TaxiEvent.class);

            return ridesAsStrings.apply(
                    "Parse from Json String",
                    FromJsonString.<TaxiEvent>builder()
                            .schema(taxiEventSchema)
                            .clz(TaxiEvent.class)
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
            PCollectionRowTuple allRows =
                    input.apply("Convert to Row", new JsonStringParser(schema()));

            PCollection<Row> rows = allRows.get(JsonStringParser.RESULTS_TAG);
            PCollection<Row> errorRows = allRows.get(JsonStringParser.ERRORS_TAG);
            PCollection<ParsingError> errorMessages =
                    errorRows.apply("Convert to ErrorMessage", new RowToError());

            // Convert row objects to input type
            PCollection<T> taxiRides =
                    rows.apply(
                            String.format("Convert to %s", clz().getSimpleName()),
                            Convert.fromRows(clz()));

            return new ParsingOutput<>(input.getPipeline(), taxiRides, errorMessages);
        }
    }

    public static class ParsingOutput<T> implements POutput {

        private final Pipeline p;
        private final PCollection<T> parsed;
        private final PCollection<ParsingError> errors;
        private final TupleTag<T> parsedTag = new TupleTag<>() {};
        private final TupleTag<ParsingError> errorsTag = new TupleTag<>() {};

        public ParsingOutput(Pipeline p, PCollection<T> parsed, PCollection<ParsingError> errors) {
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

        public PCollection<ParsingError> getErrors() {
            return errors;
        }
    }

    private static class JsonStringParser
            extends PTransform<PCollection<String>, PCollectionRowTuple> {

        public static final String RESULTS_TAG = "RESULTS_TAG";
        public static final String ERRORS_TAG = "ERRORS_TAG";
        private final Schema schema;

        public JsonStringParser(Schema schema) {
            this.schema = schema;
        }

        @Override
        public PCollectionRowTuple expand(PCollection<String> input) {

            PCollection<String> sanitized =
                    input.apply("Sanitize JSON", ParDo.of(new SanitizeJson(this.schema)));

            JsonToRow.ParseResult parseResult =
                    sanitized.apply(
                            "Parse JSON",
                            JsonToRow.withExceptionReporting(this.schema).withExtendedErrorInfo());

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

    private static class SanitizeJson extends DoFn<String, String> {
        final Schema schema;
        ObjectMapper mapper;

        public SanitizeJson(Schema schema) {
            this.schema = schema;
        }

        @Setup
        public void setup() {
            mapper = new ObjectMapper();
        }

        @ProcessElement
        public void processElement(@Element String jsonStr, OutputReceiver<String> receiver) {
            try {
                JsonNode jsonNode = mapper.readTree(jsonStr);
                JsonNode sanitized = sanitizeNode(jsonNode, schema);
                String sanitizedJsonStr = sanitized.toString();
                receiver.output(sanitizedJsonStr);
            } catch (JsonProcessingException e) {
                // Just pass the string to the next transformation, and let that one report the
                // JSON parsing
                // error
                receiver.output(jsonStr);
            }
        }

        private JsonNode sanitizeNode(JsonNode node, Schema schema) {
            List<Field> fields = schema.getFields();
            ObjectNode jsonNode = JsonNodeFactory.instance.objectNode();

            for (Field f : fields) {
                String fieldName = f.getName();
                JsonNode childNode = node.get(fieldName);
                Field childField = schema.getField(fieldName);
                TypeName typeName = f.getType().getTypeName();

                if (typeName.isCompositeType()) {
                    // Recursive iteration if this is a Composite
                    Schema childSchema =
                            Objects.requireNonNull(childField.getType().getRowSchema());
                    jsonNode.set(fieldName, sanitizeNode(childNode, childSchema));
                } else if (typeName.isCollectionType()) {
                    // Add array if this is a collection. We assume all the elements share the same
                    // collection
                    // type.
                    FieldType wrappedType =
                            Objects.requireNonNull(f.getType().getCollectionElementType());
                    assert childNode.isArray();
                    Stream<JsonNode> stream = StreamSupport.stream(childNode.spliterator(), false);
                    List<JsonNode> objects;
                    if (wrappedType.getTypeName().isCompositeType()
                            || wrappedType.getTypeName().isCollectionType()) {
                        // If the collection type is a Row/Struct, or it is a list of lists
                        Schema collectionSchema =
                                Objects.requireNonNull(wrappedType.getRowSchema());
                        objects =
                                stream.map(n -> sanitizeNode(n, collectionSchema))
                                        .collect(Collectors.toList());
                    } else {
                        // If the collection type is a single value type
                        objects =
                                stream.map(n -> sanitizeSingleNode(n, wrappedType))
                                        .collect(Collectors.toList());
                    }
                    ArrayNode sanitizedArray = jsonNode.arrayNode().addAll(objects);
                    jsonNode.set(fieldName, sanitizedArray);
                } else {
                    // Single type field
                    JsonNode sanitized = sanitizeSingleNode(childNode, childField.getType());
                    jsonNode.set(fieldName, sanitized);
                }
            }

            return jsonNode;
        }

        private JsonNode sanitizeSingleNode(JsonNode node, FieldType type) {
            TypeName name = type.getTypeName();
            if (name == TypeName.DOUBLE || name == TypeName.FLOAT) {
                return JsonNodeFactory.instance.numberNode(Double.valueOf(node.asText()));
            } else if (name == TypeName.INT64 || name == TypeName.INT32 || name == TypeName.INT16) {
                return JsonNodeFactory.instance.numberNode(Integer.valueOf(node.asText()));
            } else if (name == TypeName.BOOLEAN) {
                return JsonNodeFactory.instance.booleanNode(Boolean.parseBoolean(node.asText()));
            }

            return node;
        }
    }
}
