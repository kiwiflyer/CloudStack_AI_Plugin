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

import org.apache.cloudstack.api.BaseResponse;
import org.apache.cloudstack.api.response.ListResponse;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;

/**
 * A command that lists what the engine returns (SPEC.md section 9).
 *
 * The engine has already filtered the collection by caller context, so
 * the count here is the caller's count — which is what makes "two
 * tenants cannot see each other's endpoints" (section 14) a property of
 * one place rather than of every list command.
 */
public abstract class AiServicesListCmd<T extends BaseResponse> extends AiServicesSyncCmd {

    /** The JSON array field holding the items, for example "models". */
    protected abstract String itemsKey();

    /** Map one engine object onto a CloudStack response. */
    protected abstract T mapItem(JsonObject item);

    @Override
    protected void onSuccess(JsonObject body) {
        List<T> items = new ArrayList<>();
        if (body.has(itemsKey()) && body.get(itemsKey()).isJsonArray()) {
            JsonArray entries = body.getAsJsonArray(itemsKey());
            for (JsonElement element : entries) {
                if (element.isJsonObject()) {
                    items.add(mapItem(element.getAsJsonObject()));
                }
            }
        }

        ListResponse<T> response = new ListResponse<>();
        response.setResponses(items, items.size());
        response.setResponseName(getCommandName());
        setResponseObject(response);
    }
}
