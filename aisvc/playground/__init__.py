"""The tenant playground (SPEC.md section 7).

Two things the engine owns: the short-lived key the console talks with,
and the snippets a tenant copies out. The console itself is a UI screen
(milestone 6); everything here is what it calls.

The key is short-lived and model-scoped on purpose. A console key lives
in a browser, which is the least trustworthy place a credential can be,
so it is worth little if taken: fifteen minutes, one model, one user's
name on it. The tenant's real keys never go near the page.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aisvc.db import KeyStore
    from aisvc.db.models import ApiKeyRecord
    from aisvc.gateway import KeySecret

__all__ = [
    "SESSION_KEY_PREFIX",
    "SESSION_KEY_TTL",
    "Snippet",
    "is_session_key",
    "issue_session_key",
    "snippets_for",
]

#: CLAUDE.md: "Playground keys expire in 15 minutes."
SESSION_KEY_TTL = timedelta(minutes=15)

#: Names a key as a console session, so the tenant's key list does not
#: fill up with them and so they can be swept.
SESSION_KEY_PREFIX = "playground"

#: Snippets read the key from the environment rather than carrying one.
#: Copied code ends up in tickets, chats and repositories, and a live
#: key pasted there outlives any session.
KEY_ENV_VAR = "AISVC_API_KEY"


@dataclass(frozen=True, slots=True)
class Snippet:
    """One copy-ready example, ready to paste."""

    name: str
    language: str
    code: str


def issue_session_key(
    store: KeyStore,
    *,
    account: str,
    domain: str,
    model: str,
    user_id: str,
) -> KeySecret:
    """Mint the short-lived key the playground console uses.

    Scoped to the one model the console is open on: if the key leaks out
    of the browser it cannot be pointed at anything else, and it stops
    working within the quarter hour either way.
    """
    return store.issue(
        account=account,
        domain=domain,
        name=f"{SESSION_KEY_PREFIX}: {model} for {user_id}",
        allowed_models=(model,),
        expires_at=datetime.now(UTC) + SESSION_KEY_TTL,
    )


def is_session_key(record: ApiKeyRecord) -> bool:
    """Whether this key is a console session rather than a tenant's own."""
    return record.name.startswith(SESSION_KEY_PREFIX)


def snippets_for(*, base_url: str, model: str) -> list[Snippet]:
    """Copy-ready calls against one deployment (SPEC.md section 7)."""
    return [
        Snippet(
            name="curl",
            language="bash",
            code=f"""curl {base_url}/chat/completions \\
  -H "Authorization: Bearer ${KEY_ENV_VAR}" \\
  -H "Content-Type: application/json" \\
  -d '{{
    "model": "{model}",
    "messages": [{{"role": "user", "content": "Hello"}}]
  }}'""",
        ),
        Snippet(
            name="python",
            language="python",
            code=f'''import os

import httpx

response = httpx.post(
    "{base_url}/chat/completions",
    headers={{"Authorization": f"Bearer {{os.environ['{KEY_ENV_VAR}']}}"}},
    json={{
        "model": "{model}",
        "messages": [{{"role": "user", "content": "Hello"}}],
    }},
    timeout=60,
)
print(response.json()["choices"][0]["message"]["content"])''',
        ),
        Snippet(
            name="openai-python",
            language="python",
            code=f'''import os

from openai import OpenAI

# base_url matters: without it the SDK talks to OpenAI, not to us.
client = OpenAI(
    base_url="{base_url}",
    api_key=os.environ["{KEY_ENV_VAR}"],
)

completion = client.chat.completions.create(
    model="{model}",
    messages=[{{"role": "user", "content": "Hello"}}],
)
print(completion.choices[0].message.content)''',
        ),
        Snippet(
            name="openai-javascript",
            language="javascript",
            code=f"""import OpenAI from "openai";

// baseURL matters: without it the SDK talks to OpenAI, not to us.
const client = new OpenAI({{
  baseURL: "{base_url}",
  apiKey: process.env.{KEY_ENV_VAR},
}});

const completion = await client.chat.completions.create({{
  model: "{model}",
  messages: [{{ role: "user", content: "Hello" }}],
}});
console.log(completion.choices[0].message.content);""",
        ),
    ]
