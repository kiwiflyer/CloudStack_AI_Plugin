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

import java.util.ArrayList;
import java.util.List;

import javax.inject.Inject;

import org.apache.cloudstack.ai.api.command.ListAiModelsCmd;
import org.apache.cloudstack.ai.engine.CallerContext;
import org.apache.cloudstack.ai.engine.EngineClient;
import org.apache.cloudstack.ai.engine.EngineTransport;
import org.apache.cloudstack.ai.engine.HttpEngineTransport;
import org.apache.cloudstack.context.CallContext;
import org.apache.cloudstack.framework.config.ConfigKey;
import org.apache.cloudstack.framework.config.Configurable;

import com.cloud.user.Account;
import com.cloud.user.User;
import com.cloud.utils.component.ManagerBase;

/**
 * Registers the plugin's commands and configuration (SPEC.md section 9).
 *
 * The command list is generated from the engine's command map
 * (aisvc/api/commands.py) rather than maintained here by hand, so a
 * command cannot exist on one side and be missing from the other.
 */
public class AiServicesManagerImpl extends ManagerBase
        implements AiServicesService, Configurable {

    private EngineClient engineClient;
    private EngineTransport transport;

    @Override
    public List<Class<?>> getCommands() {
        List<Class<?>> commands = new ArrayList<>();
        commands.add(ListAiModelsCmd.class);
        // Milestones 7-9 add the remaining section 9 commands; the full
        // list and its role permissions are generated from
        // aisvc/api/commands.py.
        return commands;
    }

    @Override
    public String getConfigComponentName() {
        return AiServicesManagerImpl.class.getSimpleName();
    }

    @Override
    public ConfigKey<?>[] getConfigKeys() {
        return AiServicesConfig.all();
    }

    @Override
    public boolean isEnabledInZone(long zoneId) {
        return Boolean.TRUE.equals(AiServicesConfig.Enabled.valueIn(zoneId));
    }

    @Override
    public synchronized EngineClient getEngineClient() {
        if (engineClient == null) {
            List<String> urls = EngineClient.parseUrls(AiServicesConfig.EngineUrls.value());
            engineClient = new EngineClient(urls, resolveTransport());
        }
        return engineClient;
    }

    private EngineTransport resolveTransport() {
        if (transport != null) {
            return transport;
        }
        return new HttpEngineTransport(
                AiServicesConfig.EngineTimeout.value(),
                AiServicesConfig.EngineClientCert.value(),
                AiServicesConfig.EngineClientKey.value(),
                AiServicesConfig.EngineTrustStore.value());
    }

    /** Injection point for tests; production uses the mTLS transport. */
    public synchronized void setTransport(EngineTransport transport) {
        this.transport = transport;
        this.engineClient = null;
    }

    /**
     * Build the caller context from the CloudStack session.
     *
     * Never from request parameters: the engine trusts this assertion and
     * filters every response by it, so a caller able to influence it could
     * read another tenant's data.
     */
    @Override
    public CallerContext currentCaller() {
        CallContext context = CallContext.current();
        if (context == null) {
            throw new IllegalStateException("no CloudStack call context on this thread");
        }
        Account account = context.getCallingAccount();
        User user = context.getCallingUser();
        return new CallerContext(
                String.valueOf(user.getUuid()),
                account.getAccountName(),
                context.getCallingAccount().getDomainId() == 1L ? "/" : domainPathOf(account),
                roleNameOf(account));
    }

    private String domainPathOf(Account account) {
        // The domain path is resolved by the caller's domain, which
        // CloudStack exposes through the domain DAO. Populated when the
        // ownership table lands with the remaining commands.
        return "/" + account.getDomainId();
    }

    private String roleNameOf(Account account) {
        switch (account.getType()) {
            case ADMIN:
                return "Admin";
            case DOMAIN_ADMIN:
            case RESOURCE_DOMAIN_ADMIN:
                return "DomainAdmin";
            default:
                return "User";
        }
    }

    @Inject
    public void setEngineClientForTesting(EngineClient client) {
        this.engineClient = client;
    }
}
