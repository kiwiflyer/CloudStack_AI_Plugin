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
"""GPU node userdata (SPEC.md section 13).

Userdata carries a cluster join token, so `GpuNodeUserdata` deliberately
keeps its secrets out of `repr()`: these objects end up in log lines and
plan output.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from aisvc.k8s import gpu_resource_name

__all__ = [
    "MAX_USERDATA_BYTES",
    "GpuNodeUserdata",
    "encode_userdata",
    "render_gpu_node_userdata",
]

TEMPLATE_DIR = Path(__file__).parent.parent / "templates" / "cloud-init"

#: CloudStack accepts at most 32 KB of base64 userdata on most releases.
MAX_USERDATA_BYTES = 32 * 1024


@dataclass(frozen=True, slots=True)
class GpuNodeUserdata:
    """Everything a GPU node needs to identify itself and join the cluster."""

    hostname: str
    pool: str
    gpu_class: str
    zone: str
    cks_cluster: str
    #: Secrets: excluded from repr so they cannot reach logs or plan output.
    join_endpoint: str = field(repr=False)
    join_token: str = field(repr=False)
    ca_cert_hash: str = field(repr=False)
    weight_cache_device: str | None = None
    #: From the node's GPU class. Decides which driver the node proves is
    #: working before it joins, and which extended resource it advertises.
    vendor: str = "nvidia"


@cache
def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=False,
        autoescape=False,
    )


def render_gpu_node_userdata(userdata: GpuNodeUserdata) -> str:
    """Render the cloud-config document for one GPU node."""
    template = _environment().get_template("gpu-node.yaml.j2")
    return template.render(
        hostname=userdata.hostname,
        pool=userdata.pool,
        gpu_class=userdata.gpu_class,
        zone=userdata.zone,
        cks_cluster=userdata.cks_cluster,
        join_endpoint=userdata.join_endpoint,
        join_token=userdata.join_token,
        ca_cert_hash=userdata.ca_cert_hash,
        weight_cache_device=userdata.weight_cache_device,
        vendor=userdata.vendor,
        gpu_resource=gpu_resource_name(userdata.vendor),
    )


def encode_userdata(text: str) -> str:
    """Base64-encode userdata for `deployVirtualMachine`."""
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    if len(encoded) > MAX_USERDATA_BYTES:
        raise ValueError(
            f"userdata is too large: {len(encoded)} bytes encoded, limit is {MAX_USERDATA_BYTES}"
        )
    return encoded
