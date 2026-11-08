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
package org.apache.cloudstack.ai.engine;

import java.io.FileInputStream;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.security.KeyStore;
import java.security.SecureRandom;

import javax.net.ssl.KeyManagerFactory;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManagerFactory;

/** Builds the SSLContext the plugin presents to the engine. */
public final class MutualTls {

    private MutualTls() {
    }

    /**
     * @throws EngineException when mTLS is not fully configured. Refusing
     *         to start beats quietly connecting without a client
     *         certificate, which would let the engine trust an
     *         unauthenticated caller's asserted identity.
     */
    public static SSLContext contextFor(
            String keyStorePath, String keyStorePassword, String trustStorePath) {
        requireConfigured(keyStorePath, "ai.services.engine.tls.client.cert");
        requireConfigured(trustStorePath, "ai.services.engine.tls.truststore");

        try {
            KeyStore keyStore = load(keyStorePath, keyStorePassword);
            KeyManagerFactory keyManagers =
                    KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm());
            keyManagers.init(keyStore, charsOf(keyStorePassword));

            KeyStore trustStore = load(trustStorePath, null);
            TrustManagerFactory trustManagers =
                    TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
            trustManagers.init(trustStore);

            SSLContext context = SSLContext.getInstance("TLSv1.3");
            context.init(
                    keyManagers.getKeyManagers(),
                    trustManagers.getTrustManagers(),
                    new SecureRandom());
            return context;
        } catch (EngineException passthrough) {
            throw passthrough;
        } catch (Exception failure) {
            throw new EngineException(
                    "could not build the mTLS context for the AI Services engine: "
                            + failure.getMessage(),
                    failure);
        }
    }

    private static void requireConfigured(String value, String setting) {
        if (value == null || value.trim().isEmpty()) {
            throw new EngineException(
                    "mTLS is required to call the AI Services engine; set " + setting);
        }
        if (!Files.isReadable(Paths.get(value))) {
            throw new EngineException(setting + " points at an unreadable file: " + value);
        }
    }

    private static KeyStore load(String path, String password) throws Exception {
        KeyStore store = KeyStore.getInstance(KeyStore.getDefaultType());
        try (InputStream in = new FileInputStream(path)) {
            store.load(in, charsOf(password));
        }
        return store;
    }

    private static char[] charsOf(String password) {
        return password == null ? new char[0] : password.toCharArray();
    }
}
