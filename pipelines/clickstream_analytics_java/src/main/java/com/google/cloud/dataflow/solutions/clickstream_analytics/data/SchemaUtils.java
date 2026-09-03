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
package com.google.cloud.dataflow.solutions.clickstream_analytics.data;

import com.google.common.io.Resources;
import java.nio.charset.StandardCharsets;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public final class SchemaUtils {

    private static final Logger LOG = LoggerFactory.getLogger(SchemaUtils.class);
    public static final String DEADLETTER_SCHEMA_FILE_PATH =
            "streaming_source_deadletter_table_schema.json";

    private SchemaUtils() {}

    public static String getDeadletterTableSchemaJson() {
        try {
            return Resources.toString(
                    Resources.getResource(DEADLETTER_SCHEMA_FILE_PATH), StandardCharsets.UTF_8);
        } catch (Exception e) {
            LOG.error(
                    "Unable to read {} file from resources folder: {}",
                    DEADLETTER_SCHEMA_FILE_PATH,
                    e.getMessage());
            return null;
        }
    }
}
