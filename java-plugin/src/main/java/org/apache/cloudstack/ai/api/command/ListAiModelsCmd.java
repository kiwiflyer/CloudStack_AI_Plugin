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

import java.util.ArrayList;
import java.util.List;

import javax.inject.Inject;

import org.apache.cloudstack.ai.AiServicesService;
import org.apache.cloudstack.ai.api.response.AiModelResponse;
import org.apache.cloudstack.ai.engine.EngineException;
import org.apache.cloudstack.ai.engine.EngineResponse;
import org.apache.cloudstack.api.APICommand;
import org.apache.cloudstack.api.ApiErrorCode;
import org.apache.cloudstack.api.BaseCmd;
import org.apache.cloudstack.api.ServerApiException;
import org.apache.cloudstack.api.response.ListResponse;
import org.apache.cloudstack.context.CallContext;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

/**
 * listAiModels (SPEC.md section 9).
 *
 * Available to every role: the engine filters the catalog by the caller
 * context this command passes it, so visibility is decided there rather
 * than duplicated here.
 */
@APICommand(name = ListAiModelsCmd.APINAME,
        description = "Lists AI models published to the caller.",
        responseObject = AiModelResponse.class,
        requestHasSensitiveInfo = false,
        responseHasSensitiveInfo = false)
public class ListAiModelsCmd extends BaseCmd {

    public static final String APINAME = "listAiModels";

    @Inject
    private AiServicesService aiServices;

    @Override
    public void execute() throws ServerApiException {
        EngineResponse answer;
        try {
            answer = aiServices.getEngineClient()
                    .get("/v1/models", aiServices.currentCaller());
        } catch (EngineException failure) {
            throw new ServerApiException(
                    failure.isUnreachable()
                            ? ApiErrorCode.INTERNAL_ERROR
                            : ApiErrorCode.PARAM_ERROR,
                    failure.getMessage());
        }

        if (!answer.isSuccess()) {
            throw new ServerApiException(ApiErrorCode.INTERNAL_ERROR,
                    "the AI Services engine returned " + answer.getStatusCode());
        }

        ListResponse<AiModelResponse> response = new ListResponse<>();
        List<AiModelResponse> models = parse(answer.getBody());
        response.setResponses(models, models.size());
        response.setResponseName(getCommandName());
        setResponseObject(response);
    }

    static List<AiModelResponse> parse(String body) {
        List<AiModelResponse> models = new ArrayList<>();
        JsonObject root = JsonParser.parseString(body).getAsJsonObject();
        if (!root.has("models")) {
            return models;
        }
        JsonArray entries = root.getAsJsonArray("models");
        for (JsonElement element : entries) {
            JsonObject entry = element.getAsJsonObject();
            AiModelResponse model = new AiModelResponse();
            model.setName(string(entry, "name"));
            model.setTask(string(entry, "task"));
            model.setEngine(string(entry, "engine"));
            model.setLicense(string(entry, "license"));
            model.setPriceTier(string(entry, "price_tier"));
            if (entry.has("max_model_len") && !entry.get("max_model_len").isJsonNull()) {
                model.setMaxModelLen(entry.get("max_model_len").getAsInt());
            }
            model.setMirrored(entry.has("mirrored") && entry.get("mirrored").getAsBoolean());
            model.setObjectName("aimodel");
            models.add(model);
        }
        return models;
    }

    private static String string(JsonObject entry, String field) {
        return entry.has(field) && !entry.get(field).isJsonNull()
                ? entry.get(field).getAsString()
                : null;
    }

    @Override
    public long getEntityOwnerId() {
        return CallContext.current().getCallingAccount().getId();
    }
}
