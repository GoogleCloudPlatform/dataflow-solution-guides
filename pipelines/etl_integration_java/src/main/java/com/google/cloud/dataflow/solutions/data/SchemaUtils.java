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

package com.google.cloud.dataflow.solutions.data;

import org.apache.beam.sdk.Pipeline;
import org.apache.beam.sdk.schemas.NoSuchSchemaException;
import org.apache.beam.sdk.schemas.Schema;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class SchemaUtils {

    private static final Logger LOG = LoggerFactory.getLogger(SchemaUtils.class);

    public static <T> Schema getSchemaForType(Pipeline p, Class<T> classType) {
        Schema schema;

        try {
            schema = p.getSchemaRegistry().getSchema(classType);
        } catch (NoSuchSchemaException e) {
            LOG.error(e.getMessage());
            throw new IllegalArgumentException(
                    String.format(
                            "Could not find schema for %s",
                            TaxiObjects.TaxiEvent.class.getCanonicalName()));
        }

        return schema;
    }
}
