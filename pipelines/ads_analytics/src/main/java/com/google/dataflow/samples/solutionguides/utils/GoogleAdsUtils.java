package com.google.dataflow.samples.solutionguides.utils;

import com.google.ads.googleads.v17.services.GoogleAdsRow;
import com.google.api.services.bigquery.model.TableFieldSchema;
import com.google.api.services.bigquery.model.TableSchema;
import com.google.common.base.Functions;
import com.google.common.base.Splitter;
import com.google.common.base.Strings;
import com.google.common.collect.ImmutableMap;
import com.google.common.collect.Streams;
import com.google.protobuf.Descriptors;
import javax.annotation.Nullable;
import java.util.Arrays;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Collectors;

import com.google.cloud.bigquery.LegacySQLTypeName;

import static java.util.stream.Collectors.toList;

/**
 * Various utility functions to assist in parsing and transforming <a
 * href="https://developers.google.com/google-ads/api/docs/query/overview">Google Ads Query Language
 * queries</a>.
 */
public class GoogleAdsUtils {

    // This regex matches the minimal valid query grammar for the Google Ads Query Language.
    // See https://developers.google.com/google-ads/api/docs/query/grammar for details.
    private static final String SELECT = "\\b(?i:SELECT)\\b";
    private static final String FIELD_NAME = "[a-z][a-zA-Z0-9._]*";
    private static final String COMMA = "\\s*,\\s*";
    private static final String SELECT_CLAUSE =
            String.format("%s\\s*(%s(?:%s%s)*)", SELECT, FIELD_NAME, COMMA, FIELD_NAME);
    private static final String FROM = "\\b(?i:FROM)\\b";
    private static final String RESOURCE_NAME = "[a-z][a-zA-Z_]*";
    private static final String FROM_CLAUSE = String.format("%s\\s*(%s)", FROM, RESOURCE_NAME);
    private static final String MINIMAL_QUERY =
            String.format("\\s*%s\\s*%s", SELECT_CLAUSE, FROM_CLAUSE);
    private static final Pattern MINIMAL_QUERY_PATTERN = Pattern.compile(MINIMAL_QUERY);

    private static final Splitter FIELD_PATH_SPLITTER = Splitter.on('.');

    private static final Descriptors.Descriptor GOOGLE_ADS_ROW_DESCRIPTOR =
            GoogleAdsRow.getDescriptor();

    // See https://cloud.google.com/bigquery/docs/nested-repeated#limitations
    private static final int MAX_BIG_QUERY_RECORD_NESTING = 15;

    private static String[] parseSelectedFields(String query) {
        Matcher matcher = MINIMAL_QUERY_PATTERN.matcher(query);

        if (!matcher.lookingAt()) {
            throw new IllegalArgumentException(
                    String.format("Query has an invalid SELECT or FROM clause: %s", query));
        }

        return matcher.group(1).split(COMMA);
    }

    public static Map<String, Descriptors.FieldDescriptor> getSelectedFieldDescriptors(String query) {
        return Arrays.stream(parseSelectedFields(query))
                .collect(
                        ImmutableMap.toImmutableMap(
                                Functions.identity(),
                                field -> {
                                    Iterator<String> fieldPath = FIELD_PATH_SPLITTER.split(field).iterator();
                                    String fieldName = fieldPath.next();
                                    return Streams.stream(fieldPath)
                                            .reduce(
                                                    GOOGLE_ADS_ROW_DESCRIPTOR.findFieldByName(fieldName),
                                                    (fieldDescriptor, name) ->
                                                            fieldDescriptor.getMessageType().findFieldByName(name),
                                                    (prev, next) -> next);
                                }));
    }

    public static TableSchema createBigQuerySchema(String query) {
        return new TableSchema()
                .setFields(
                        getSelectedFieldDescriptors(query).entrySet().stream()
                                .map(
                                        entry ->
                                                convertProtoFieldDescriptorToBigQueryField(
                                                                entry.getValue(), true, null, 1)
                                                        .setName(entry.getKey().replace('.', '_')))
                                .collect(Collectors.toList()));
    }

    /** Handlers proto field to BigQuery field conversion. */
    public static TableFieldSchema convertProtoFieldDescriptorToBigQueryField(
            Descriptors.FieldDescriptor fieldDescriptor,
            boolean preserveProtoFieldNames,
            @Nullable Descriptors.FieldDescriptor parent,
            int nestingLevel) {

        TableFieldSchema schema = new TableFieldSchema();

        String jsonName = fieldDescriptor.getJsonName();
        schema.setName(
                preserveProtoFieldNames || Strings.isNullOrEmpty(jsonName)
                        ? fieldDescriptor.getName()
                        : jsonName);

        LegacySQLTypeName sqlType = convertProtoTypeToSqlType(fieldDescriptor.getJavaType());
        schema.setType(sqlType.toString());

        if (sqlType == LegacySQLTypeName.RECORD) {
            if (nestingLevel > MAX_BIG_QUERY_RECORD_NESTING) {
                throw new IllegalArgumentException(
                        String.format(
                                "Record field `%s.%s` is at BigQuery's nesting limit of %s, but it contains"
                                        + " message field `%s` of type `%s`. This could be caused by circular message"
                                        + " references, including a self-referential message.",
                                parent.getMessageType().getName(),
                                parent.getName(),
                                MAX_BIG_QUERY_RECORD_NESTING,
                                fieldDescriptor.getName(),
                                fieldDescriptor.getMessageType().getName()));
            }

            List<TableFieldSchema> subFields =
                    fieldDescriptor.getMessageType().getFields().stream()
                            .map(
                                    fd ->
                                            convertProtoFieldDescriptorToBigQueryField(
                                                    fd, preserveProtoFieldNames, fieldDescriptor, nestingLevel + 1))
                            .collect(toList());
            schema.setFields(subFields);
        }

        if (fieldDescriptor.isRepeated()) {
            schema.setMode("REPEATED");
        } else if (fieldDescriptor.isRequired()) {
            schema.setMode("REQUIRED");
        } else {
            schema.setMode("NULLABLE");
        }

        return schema;
    }

    /** Handles mapping a proto type to BigQuery type. */
    private static LegacySQLTypeName convertProtoTypeToSqlType(Descriptors.FieldDescriptor.JavaType protoType) {
        switch (protoType) {
            case INT:
                // fall through
            case LONG:
                return LegacySQLTypeName.INTEGER;
            case FLOAT:
                // fall through
            case DOUBLE:
                return LegacySQLTypeName.FLOAT;
            case BOOLEAN:
                return LegacySQLTypeName.BOOLEAN;
            case ENUM:
                // fall through
            case STRING:
                return LegacySQLTypeName.STRING;
            case BYTE_STRING:
                return LegacySQLTypeName.BYTES;
            case MESSAGE:
                return LegacySQLTypeName.RECORD;
        }
        throw new IllegalArgumentException(String.format("Unrecognized type: %s", protoType));
    }
}
