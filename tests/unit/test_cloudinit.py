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
"""GPU node userdata (SPEC.md section 13, templates/cloud-init)."""

from __future__ import annotations

import base64

import pytest
import yaml

from aisvc.provision.cloudinit import (
    GpuNodeUserdata,
    encode_userdata,
    render_gpu_node_userdata,
)

USERDATA = GpuNodeUserdata(
    hostname="aisvc-h100-pool-001",
    pool="h100-pool",
    gpu_class="h100-8x",
    zone="zone-east-1",
    cks_cluster="aisvc-east-1",
    join_endpoint="10.0.0.10:6443",
    join_token="abcdef.0123456789abcdef",
    ca_cert_hash="sha256:deadbeef",
    weight_cache_device="/dev/nvme1n1",
)


class TestRendering:
    def test_output_is_a_cloud_config_document(self) -> None:
        text = render_gpu_node_userdata(USERDATA)
        assert text.startswith("#cloud-config\n")
        assert isinstance(yaml.safe_load(text), dict)

    def test_hostname_is_set(self) -> None:
        doc = yaml.safe_load(render_gpu_node_userdata(USERDATA))
        assert doc["hostname"] == "aisvc-h100-pool-001"

    def test_node_is_labelled_with_its_pool_and_class(self) -> None:
        text = render_gpu_node_userdata(USERDATA)
        assert "aisvc.io/node-pool=h100-pool" in text
        assert "aisvc.io/gpu-class=h100-8x" in text
        assert "aisvc.io/zone=zone-east-1" in text

    def test_the_node_joins_the_named_cluster(self) -> None:
        text = render_gpu_node_userdata(USERDATA)
        assert "kubeadm join 10.0.0.10:6443" in text
        assert "abcdef.0123456789abcdef" in text
        assert "sha256:deadbeef" in text

    def test_the_weight_cache_device_is_mounted(self) -> None:
        text = render_gpu_node_userdata(USERDATA)
        assert "/dev/nvme1n1" in text
        assert "/var/lib/aisvc/weights" in text

    def test_rendering_is_deterministic(self) -> None:
        assert render_gpu_node_userdata(USERDATA) == render_gpu_node_userdata(USERDATA)

    def test_no_weight_cache_device_omits_the_mount(self) -> None:
        plain = GpuNodeUserdata(
            hostname="n1",
            pool="p",
            gpu_class="c",
            zone="z",
            cks_cluster="k",
            join_endpoint="10.0.0.10:6443",
            join_token="t.t",
            ca_cert_hash="sha256:x",
            weight_cache_device=None,
        )
        text = render_gpu_node_userdata(plain)
        assert "/var/lib/aisvc/weights" not in text
        assert yaml.safe_load(text)["hostname"] == "n1"

    def test_the_gpu_driver_is_verified_before_the_node_joins(self) -> None:
        """A node that joins without a working driver takes scheduled pods."""
        doc = yaml.safe_load(render_gpu_node_userdata(USERDATA))
        # runcmd uses the exec-list form, so flatten each entry to text.
        commands = "\n".join(" ".join(entry) for entry in doc["runcmd"])
        assert commands.index("nvidia-smi") < commands.index("kubeadm join")


class TestSecrets:
    def test_userdata_is_never_in_the_dataclass_repr(self) -> None:
        """Userdata carries a join token; it must not reach logs or plans."""
        assert "abcdef.0123456789abcdef" not in repr(USERDATA)
        assert "sha256:deadbeef" not in repr(USERDATA)

    def test_the_repr_still_identifies_the_node(self) -> None:
        assert "aisvc-h100-pool-001" in repr(USERDATA)


class TestEncoding:
    def test_userdata_is_base64_encoded_for_the_api(self) -> None:
        encoded = encode_userdata("#cloud-config\nhostname: n1\n")
        assert base64.b64decode(encoded).decode() == "#cloud-config\nhostname: n1\n"

    def test_oversized_userdata_is_rejected(self) -> None:
        """CloudStack caps userdata; failing here beats a confusing API error."""
        with pytest.raises(ValueError, match="too large"):
            encode_userdata("x" * 1_000_000)
