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

import org.apache.cloudstack.ai.api.command.DeleteAiModelDeploymentCmd;
import org.apache.cloudstack.ai.api.command.DeployAiModelCmd;
import org.apache.cloudstack.ai.api.command.ListAiModelsCmd;
import org.apache.cloudstack.ai.engine.CallerContext;
import org.apache.cloudstack.ai.engine.EngineClient;
import org.apache.cloudstack.ai.dao.AiDeploymentDao;
import org.apache.cloudstack.ai.engine.EngineTransport;
import org.apache.cloudstack.ai.engine.HttpEngineTransport;
import org.apache.cloudstack.context.CallContext;
import org.apache.cloudstack.framework.config.ConfigKey;
import org.apache.cloudstack.framework.config.Configurable;

import com.cloud.domain.Domain;
import com.cloud.domain.dao.DomainDao;
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

    @Inject
    private DomainDao domainDao;

    @Inject
    private AiDeploymentDao deploymentDao;

    private EngineClient engineClient;
    private EngineTransport transport;

    @Override
    public List<Class<?>> getCommands() {
        List<Class<?>> commands = new ArrayList<>();
        commands.add(ListAiModelsCmd.class);
        commands.add(DeployAiModelCmd.class);
        commands.add(DeleteAiModelDeploymentCmd.class);
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
                domainPathOf(account.getDomainId()),
                roleNameOf(account));
    }

    /**
     * The caller's full domain path, for example /Customers/Acme.
     *
     * The engine decides domain-admin visibility by prefix-matching this
     * against a resource's domain, so a wrong or partial path either
     * hides a domain's own resources or exposes a sibling's. Resolved
     * from the domain DAO rather than assembled from an id.
     */
    String domainPathOf(long domainId) {
        Domain domain = domainDao == null ? null : domainDao.findById(domainId);
        if (domain == null || domain.getPath() == null || domain.getPath().isEmpty()) {
            throw new IllegalStateException(
                    "cannot resolve the path of domain " + domainId
                            + "; refusing to call the engine with an unknown domain");
        }
        String path = domain.getPath();
        if (!path.startsWith("/")) {
            path = "/" + path;
        }
        // CloudStack stores paths with a trailing slash; the engine
        // compares them as prefixes, where a trailing slash would make
        // /Customers/Acme/ fail to match /Customers/Acme.
        return path.length() > 1 && path.endsWith("/")
                ? path.substring(0, path.length() - 1)
                : path;
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

    /**
     * Remove an account's deployments (SPEC.md section 9).
     *
     * Only the ownership rows: the engine owns the running workloads and
     * removes them when it next reconciles, because the spec they were
     * deployed from no longer lists them. Deleting here and there would
     * race.
     *
     * NOTE: CloudStack 4.23 exposes no plugin-facing account-cleanup
     * interface — AccountCleanupTask is internal to AccountManagerImpl —
     * so this is not yet wired to account deletion. See PROGRESS.md.
     */
    @Override
    public int cleanupAccount(long accountId) {
        if (deploymentDao == null) {
            return 0;
        }
        return deploymentDao.removeByAccount(accountId);
    }

    /** Test seam for the DAO, which is otherwise Spring-injected. */
    void setDeploymentDao(AiDeploymentDao dao) {
        this.deploymentDao = dao;
    }

    /** Test seam for the domain DAO. */
    void setDomainDao(DomainDao dao) {
        this.domainDao = dao;
    }
}
