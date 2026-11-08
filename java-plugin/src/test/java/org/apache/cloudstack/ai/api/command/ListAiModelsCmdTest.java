// Licensed to the Apache Software Foundation (ASF) under one or more
// contributor license agreements. See the NOTICE file distributed with this
// work for additional information regarding copyright ownership. The ASF
// licenses this file to you under the Apache License, Version 2.0 (the
// "License"); you may not use this file except in compliance with the
// License. You may obtain a copy of the License at
//
//   http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
// WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
// License for the specific language governing permissions and limitations
// under the License.
package org.apache.cloudstack.ai.api.command;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.List;

import org.apache.cloudstack.ai.api.response.AiModelResponse;
import org.apache.cloudstack.api.APICommand;
import org.junit.jupiter.api.Test;

/**
 * Translating the engine's catalog into CloudStack responses.
 *
 * The engine speaks snake_case JSON and CloudStack responses are
 * lowercase-concatenated, so this mapping is exactly where a field goes
 * quietly missing.
 */
class ListAiModelsCmdTest {

    private static final String CATALOG =
            "{\"count\": 2, \"models\": ["
                    + "{\"name\": \"llama-3.3-70b-instruct\", \"task\": \"chat\","
                    + " \"engine\": \"vllm\", \"license\": \"llama3.3\","
                    + " \"price_tier\": \"standard\", \"max_model_len\": 32768,"
                    + " \"mirrored\": true},"
                    + "{\"name\": \"bge-m3-embedding\", \"task\": \"embedding\","
                    + " \"engine\": \"vllm\", \"license\": \"mit\","
                    + " \"price_tier\": \"embedding\", \"max_model_len\": 8192,"
                    + " \"mirrored\": false}"
                    + "]}";

    @Test
    void everyModelIsMapped() {
        List<AiModelResponse> models = ListAiModelsCmd.parse(CATALOG);

        assertEquals(2, models.size());
        assertEquals("llama-3.3-70b-instruct", models.get(0).getName());
    }

    @Test
    void theMirroredFlagSurvives() {
        // A tenant needs to know a model cannot be deployed yet.
        List<AiModelResponse> models = ListAiModelsCmd.parse(CATALOG);

        assertTrue(models.get(0).getMirrored());
        assertFalse(models.get(1).getMirrored());
    }

    @Test
    void anEmptyCatalogIsNotAnError() {
        assertEquals(0, ListAiModelsCmd.parse("{\"count\": 0, \"models\": []}").size());
    }

    @Test
    void aBodyWithoutModelsYieldsNothing() {
        assertEquals(0, ListAiModelsCmd.parse("{\"count\": 0}").size());
    }

    @Test
    void aMissingOptionalFieldIsNullNotAnError() {
        List<AiModelResponse> models =
                ListAiModelsCmd.parse("{\"models\": [{\"name\": \"m\"}]}");

        assertEquals("m", models.get(0).getName());
        assertFalse(models.get(0).getMirrored());
    }

    @Test
    void anExplicitNullIsTreatedAsAbsent() {
        List<AiModelResponse> models = ListAiModelsCmd.parse(
                "{\"models\": [{\"name\": \"m\", \"price_tier\": null,"
                        + " \"max_model_len\": null}]}");

        assertEquals(1, models.size());
        assertEquals("m", models.get(0).getName());
    }

    @Test
    void everyEntryIsNamedForTheResponseSerialiser() {
        for (AiModelResponse model : ListAiModelsCmd.parse(CATALOG)) {
            assertEquals("aimodel", model.getObjectName());
        }
    }

    @Test
    void theCommandIsAnnotatedWithTheSpecName() {
        APICommand annotation = ListAiModelsCmd.class.getAnnotation(APICommand.class);

        assertEquals("listAiModels", annotation.name());
        assertEquals(AiModelResponse.class, annotation.responseObject());
    }

    @Test
    void theCommandDeclaresNoSensitiveInfo() {
        // The catalog carries no keys or prompts, and saying so keeps it
        // out of CloudStack's sensitive-parameter redaction.
        APICommand annotation = ListAiModelsCmd.class.getAnnotation(APICommand.class);

        assertFalse(annotation.requestHasSensitiveInfo());
        assertFalse(annotation.responseHasSensitiveInfo());
    }

    @Test
    void theApiNameConstantMatchesTheAnnotation() {
        assertEquals(
                ListAiModelsCmd.class.getAnnotation(APICommand.class).name(),
                ListAiModelsCmd.APINAME);
    }

    @Test
    void anUnsetMaxModelLenStaysUnset() {
        List<AiModelResponse> models =
                ListAiModelsCmd.parse("{\"models\": [{\"name\": \"m\"}]}");

        assertNull(models.get(0).getObjectId());
    }
}
