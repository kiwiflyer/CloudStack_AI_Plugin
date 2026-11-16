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

import javax.inject.Inject;

import org.apache.cloudstack.ai.AiServicesService;
import org.apache.cloudstack.ai.engine.EngineException;
import org.apache.cloudstack.ai.engine.EnginePaths;
import org.apache.cloudstack.ai.engine.EngineResponse;
import org.apache.cloudstack.api.BaseCmd;
import org.apache.cloudstack.api.ServerApiException;
import org.apache.cloudstack.context.CallContext;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

/**
 * A command that calls the engine and answers immediately
 * (SPEC.md section 9).
 *
 * Every response is already filtered by the engine against the caller
 * context, so these commands pass identity through rather than deciding
 * visibility themselves — one place to get it right instead of thirty.
 */
public abstract class AiServicesSyncCmd extends BaseCmd {

    @Inject
    protected AiServicesService aiServices;

    protected abstract String enginePath();

    protected String engineMethod() {
        return "GET";
    }

    protected String engineBody() {
        return null;
    }

    /** Build this command's response from the engine's answer. */
    protected abstract void onSuccess(JsonObject body);

    @Override
    public void execute() throws ServerApiException {
        JsonObject body = callEngine();
        onSuccess(body);
    }

    protected JsonObject callEngine() throws ServerApiException {
        try {
            EngineResponse answer = send();
            if (!answer.isSuccess()) {
                throw new ServerApiException(
                        AiServicesAsyncCmd.errorCodeFor(
                                new EngineException("", answer.getStatusCode(), null)),
                        "the AI Services engine returned " + answer.getStatusCode()
                                + " for " + enginePath() + explain(answer.getBody()));
            }
            return parse(answer.getBody());
        } catch (EngineException failure) {
            throw new ServerApiException(
                    AiServicesAsyncCmd.errorCodeFor(failure), failure.getMessage());
        }
    }

    private EngineResponse send() {
        String path = enginePath();
        switch (engineMethod()) {
            case "POST":
                return aiServices.getEngineClient()
                        .post(path, aiServices.currentCaller(), engineBody());
            case "PATCH":
                return aiServices.getEngineClient()
                        .patch(path, aiServices.currentCaller(), engineBody());
            case "DELETE":
                return aiServices.getEngineClient().delete(path, aiServices.currentCaller());
            default:
                return aiServices.getEngineClient().get(path, aiServices.currentCaller());
        }
    }

    /**
     * The engine's own account of a failure, where it gave one.
     *
     * <p>The engine answers in RFC 9457 problem JSON so that its
     * caller can say why something failed, and this plugin is its
     * only caller. Without this the status was all an operator saw:
     * the first end-to-end round trip reported "the AI Services
     * engine returned 503 for /v1/capacity" while the engine had
     * said "capacity has not been observed yet" — the sentence that
     * names the remedy.
     *
     * <p>Appended to the status rather than replacing it, because the
     * status is what gets grepped for. A body that is not problem
     * JSON yields nothing: something other than the engine — a proxy,
     * a load balancer — may have answered, and a parse failure must
     * not displace the real one.
     */
    private static String explain(String body) {
        if (body == null || body.trim().isEmpty()) {
            return "";
        }
        try {
            JsonObject problem = JsonParser.parseString(body).getAsJsonObject();
            for (String field : new String[] {"detail", "title"}) {
                if (problem.has(field) && problem.get(field).isJsonPrimitive()) {
                    String said = problem.get(field).getAsString().trim();
                    if (!said.isEmpty()) {
                        return ": " + said;
                    }
                }
            }
        } catch (RuntimeException notProblemJson) {
            return "";
        }
        return "";
    }

    static JsonObject parse(String body) {
        if (body == null || body.trim().isEmpty()) {
            return new JsonObject();
        }
        try {
            return JsonParser.parseString(body).getAsJsonObject();
        } catch (RuntimeException malformed) {
            throw new EngineException(
                    "the AI Services engine returned an unreadable body: "
                            + malformed.getMessage());
        }
    }

    /** Append a query parameter to an engine path. */
    protected static String withQuery(String path, String name, Object value) {
        return EnginePaths.withQuery(path, name, value);
    }

    @Override
    public long getEntityOwnerId() {
        return CallContext.current().getCallingAccount().getId();
    }
}
