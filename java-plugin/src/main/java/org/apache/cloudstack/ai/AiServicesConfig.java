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
package org.apache.cloudstack.ai;

import org.apache.cloudstack.framework.config.ConfigKey;

/**
 * Global settings for the AI Services plugin (SPEC.md section 9).
 *
 * Scopes matter here: enablement is per zone because a provider may run
 * GPU capacity in one zone only, and Hugging Face approval is per domain
 * because section 5 lets a reseller mirror directly while others must
 * request it.
 */
public final class AiServicesConfig {

    private AiServicesConfig() {
    }

    private static final String CATEGORY = "Advanced";

    public static final ConfigKey<Boolean> Enabled = new ConfigKey<>(
            CATEGORY, Boolean.class, "ai.services.enabled", "false",
            "Whether AI Services is enabled in this zone.", true, ConfigKey.Scope.Zone);

    /**
     * Comma-separated engine URLs. More than one because the engine runs at
     * least two replicas behind a load balancer and the plugin fails over
     * between them (SPEC.md section 7).
     */
    public static final ConfigKey<String> EngineUrls = new ConfigKey<>(
            CATEGORY, String.class, "ai.services.engine.urls", "",
            "Comma-separated base URLs of the AI Services engine API, tried in order.",
            true, ConfigKey.Scope.Global);

    public static final ConfigKey<Integer> EngineTimeout = new ConfigKey<>(
            CATEGORY, Integer.class, "ai.services.engine.timeout", "30",
            "Seconds to wait for an engine API response before failing over.",
            true, ConfigKey.Scope.Global);

    public static final ConfigKey<String> EngineClientCert = new ConfigKey<>(
            CATEGORY, String.class, "ai.services.engine.tls.client.cert", "",
            "Path to the client certificate the plugin presents to the engine (mTLS).",
            true, ConfigKey.Scope.Global);

    public static final ConfigKey<String> EngineClientKey = new ConfigKey<>(
            CATEGORY, String.class, "ai.services.engine.tls.client.key", "",
            "Path to the private key for the client certificate (mTLS).",
            true, ConfigKey.Scope.Global);

    public static final ConfigKey<String> EngineTrustStore = new ConfigKey<>(
            CATEGORY, String.class, "ai.services.engine.tls.truststore", "",
            "Path to the trust store holding the engine's CA certificate.",
            true, ConfigKey.Scope.Global);

    /**
     * Domain-scoped: where false, a domain admin may mirror from Hugging Face
     * directly; where true, their request goes to a root admin queue
     * (SPEC.md section 5, step 4).
     */
    public static final ConfigKey<Boolean> HfRequireApproval = new ConfigKey<>(
            CATEGORY, Boolean.class, "ai.services.hf.require.approval", "true",
            "Whether domain admin mirror requests need root admin approval.",
            true, ConfigKey.Scope.Domain);

    /**
     * How long host maintenance waits for a drain before the admin must
     * confirm a forced drain (SPEC.md section 7).
     */
    public static final ConfigKey<Integer> MaintenanceDrainTimeout = new ConfigKey<>(
            CATEGORY, Integer.class, "ai.services.maintenance.drain.timeout", "900",
            "Seconds to wait for GPU nodes to drain before a host enters maintenance.",
            true, ConfigKey.Scope.Global);

    public static ConfigKey<?>[] all() {
        return new ConfigKey<?>[] {
                Enabled,
                EngineUrls,
                EngineTimeout,
                EngineClientCert,
                EngineClientKey,
                EngineTrustStore,
                HfRequireApproval,
                MaintenanceDrainTimeout,
        };
    }
}
