# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements. See the NOTICE file distributed with this
# work for additional information regarding copyright ownership. The ASF
# licenses this file to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance with the
# License. You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
"""The tenant playground (SPEC.md section 7).

"Uses a 15-minute, model-scoped session key, so real keys never reach
the browser" and CLAUDE.md's "Playground keys expire in 15 minutes" are
the two rules these hold to.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aisvc.db import KeyStore
from aisvc.playground import (
    SESSION_KEY_TTL,
    Snippet,
    issue_session_key,
    snippets_for,
)


class TestSessionKeys:
    def test_a_session_key_expires_in_fifteen_minutes(self, keys: KeyStore) -> None:
        minted = issue_session_key(keys, account="acme", domain="/d", model="llama", user_id="u-1")

        record = keys.record_for(minted.key_id)
        assert record.expires_at is not None
        remaining = record.expires_at - dt.datetime.now(dt.UTC).replace(tzinfo=None)
        assert dt.timedelta(minutes=14) < remaining <= dt.timedelta(minutes=15)

    def test_the_ttl_is_the_documented_one(self) -> None:
        assert dt.timedelta(minutes=15) == SESSION_KEY_TTL

    def test_a_session_key_is_scoped_to_one_model(self, keys: KeyStore) -> None:
        """So a leaked console key cannot reach another model."""
        minted = issue_session_key(keys, account="acme", domain="/d", model="llama", user_id="u-1")

        assert minted.key.allowed_models == ("llama",)

    def test_a_session_key_verifies_for_its_model(self, keys: KeyStore) -> None:
        minted = issue_session_key(keys, account="acme", domain="/d", model="llama", user_id="u-1")

        assert keys.verify(minted.plaintext, model="llama", source_ip="10.0.0.1")

    def test_a_session_key_is_refused_for_another_model(self, keys: KeyStore) -> None:
        from aisvc.gateway import KeyRejected

        minted = issue_session_key(keys, account="acme", domain="/d", model="llama", user_id="u-1")

        with pytest.raises(KeyRejected):
            keys.verify(minted.plaintext, model="gemma", source_ip="10.0.0.1")

    def test_the_key_says_who_opened_the_console(self, keys: KeyStore) -> None:
        """Playground usage is metered like any other; it needs an owner."""
        minted = issue_session_key(keys, account="acme", domain="/d", model="llama", user_id="u-1")

        assert "u-1" in minted.key.name

    def test_a_session_key_is_marked_as_one(self, keys: KeyStore) -> None:
        minted = issue_session_key(keys, account="acme", domain="/d", model="llama", user_id="u-1")

        assert keys.record_for(minted.key_id).name.startswith("playground")

    def test_session_keys_are_listed_apart_from_real_ones(self, keys: KeyStore) -> None:
        """A tenant's key list should not fill up with console sessions."""
        keys.issue(account="acme", domain="/d", name="ci")
        issue_session_key(keys, account="acme", domain="/d", model="llama", user_id="u")

        from aisvc.playground import is_session_key

        listed = keys.list_for(account="acme", domain="/d")
        assert [k.name for k in listed if not is_session_key(k)] == ["ci"]


class TestSnippets:
    def snippets(self, **kw: str) -> list[Snippet]:
        defaults = {
            "base_url": "https://ai.example.com/v1",
            "model": "llama-3.3-70b-instruct",
        }
        defaults.update(kw)
        return snippets_for(**defaults)

    def test_every_documented_language_is_covered(self) -> None:
        """SPEC.md section 7: curl, Python, and the OpenAI SDK for both."""
        assert {s.name for s in self.snippets()} == {
            "curl",
            "python",
            "openai-python",
            "openai-javascript",
        }

    def test_the_base_url_is_filled_in(self) -> None:
        assert all("https://ai.example.com/v1" in s.code for s in self.snippets())

    def test_the_model_is_filled_in(self) -> None:
        assert all("llama-3.3-70b-instruct" in s.code for s in self.snippets())

    def test_no_snippet_embeds_a_real_key(self) -> None:
        """A snippet is copied into chats and tickets; a live key must not be.

        The console holds the session key; what the tenant copies reads
        it from the environment instead.
        """
        for snippet in self.snippets():
            assert "aisvc-" not in snippet.code
            assert "AISVC_API_KEY" in snippet.code

    def test_each_snippet_says_what_language_it_is(self) -> None:
        assert all(s.language for s in self.snippets())

    def test_the_curl_snippet_posts_to_chat_completions(self) -> None:
        curl = next(s for s in self.snippets() if s.name == "curl")

        assert "/chat/completions" in curl.code
        assert "Authorization: Bearer" in curl.code

    def test_the_openai_snippets_point_at_our_base_url(self) -> None:
        """The default would send the tenant's prompts to OpenAI."""
        for name in ("openai-python", "openai-javascript"):
            snippet = next(s for s in self.snippets() if s.name == name)
            assert "base_url" in snippet.code or "baseURL" in snippet.code
