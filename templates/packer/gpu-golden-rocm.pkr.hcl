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
// Golden GPU node template for AMD/ROCm nodes (SPEC.md sections 7 and 13).
//
// The ROCm sibling of gpu-golden.pkr.hcl, for a gpuClass declaring
// `vendor: amd`. Same contract: everything slow is baked in so cloud-init
// only identifies the node and joins, and /etc/aisvc/image.json records what
// is in the image so fleet health can spot drift.
//
// Two differences from the NVIDIA template are worth knowing before editing:
//
//   - There is no container toolkit. ROCm needs no runtime shim: the AMD
//     GPU device plugin hands /dev/kfd and /dev/dri to the container, so
//     containerd is installed stock and left alone. The node's cloud-init
//     skips `nvidia-ctk` and the containerd restart for the same reason.
//   - The driver and the ROCm userspace come from two repositories that
//     version INDEPENDENTLY. repo.radeon.com/amdgpu carries both a
//     ROCm-aligned series (6.4.x, 7.0.x) and a driver series (30.x, 31.x),
//     while repo.radeon.com/rocm/apt runs ahead — at the time of writing
//     rocm/apt/7.2.4 exists and amdgpu/7.2.4 is a 404. So they are two
//     variables, not one, and a matched pair has to be checked rather than
//     assumed. 7.0.3 is a pair that resolves for noble on both.
//
// NEVER BUILT. The repository paths and every package name below were
// checked against repo.radeon.com, but no AMD image has been baked and no
// AMD node has booted. Treat a first build as the thing that finds the gaps.

packer {
  required_plugins {
    cloudstack = {
      # Matches gpu-golden.pkr.hcl: the plugin's newest release is 1.0.2.
      version = ">= 1.0.2"
      source  = "github.com/hashicorp/cloudstack"
    }
  }
}

variable "image_version" {
  type        = string
  description = "Template name, e.g. gpu-golden-rocm-2026.11. Referenced by nodePools[].template."
}

variable "amdgpu_version" {
  type        = string
  description = "repo.radeon.com/amdgpu series for the kernel driver. Not the same number as rocm_version."
  default     = "7.0.3"
}

variable "rocm_version" {
  type        = string
  description = "repo.radeon.com/rocm/apt series for amd-smi and rocm-smi."
  default     = "7.0.3"
}

variable "kubernetes_version" {
  type    = string
  default = "1.31.4"
}

variable "base_template" {
  type        = string
  description = "Ubuntu 24.04 LTS base template already registered in the zone."
  default     = "ubuntu-24.04-server"
}

variable "cloudstack_api_url" {
  type = string
}

variable "cloudstack_api_key" {
  type      = string
  sensitive = true
}

variable "cloudstack_secret_key" {
  type      = string
  sensitive = true
}

variable "zone" {
  type = string
}

variable "build_service_offering" {
  type        = string
  description = "A plain CPU offering; no GPU is needed to bake the image."
  default     = "Medium-Instance"
}

variable "network" {
  type        = string
  description = "Network with egress, for package downloads during the build."
}

source "cloudstack" "gpu" {
  api_url    = var.cloudstack_api_url
  api_key    = var.cloudstack_api_key
  secret_key = var.cloudstack_secret_key

  zone             = var.zone
  source_template  = var.base_template
  service_offering = var.build_service_offering
  network          = var.network

  ssh_username = "ubuntu"
  expunge      = true

  template_name         = var.image_version
  template_display_text = "CloudStack AI Services GPU node (ROCm) ${var.image_version}"
  template_os           = "Ubuntu 24.04 LTS"
  template_featured     = false
  template_public       = false
  template_scalable     = true
}

build {
  name    = "aisvc-gpu-node-rocm"
  sources = ["source.cloudstack.gpu"]

  provisioner "shell" {
    inline = [
      "set -euxo pipefail",
      "cloud-init status --wait || true",
      "sudo apt-get update",
      "sudo apt-get install -y --no-install-recommends ca-certificates curl gnupg jq nvme-cli",
    ]
  }

  // AMD kernel driver and the SMI tooling the node's boot check uses.
  // One signing key covers both repositories.
  provisioner "shell" {
    inline = [
      "set -euxo pipefail",
      "sudo mkdir -p --mode=0755 /etc/apt/keyrings",
      "curl -fsSL https://repo.radeon.com/rocm/rocm.gpg.key | sudo gpg --dearmor -o /etc/apt/keyrings/rocm.gpg",
      "echo 'deb [arch=amd64 signed-by=/etc/apt/keyrings/rocm.gpg] https://repo.radeon.com/amdgpu/${var.amdgpu_version}/ubuntu noble main' | sudo tee /etc/apt/sources.list.d/amdgpu.list",
      "echo 'deb [arch=amd64 signed-by=/etc/apt/keyrings/rocm.gpg] https://repo.radeon.com/rocm/apt/${var.rocm_version} noble main' | sudo tee /etc/apt/sources.list.d/rocm.list",
      "sudo apt-get update",
      // amdgpu-dkms builds against the running kernel, so the image is tied
      // to the kernel it was baked on; a base template kernel bump needs a
      // rebuild, which image.json makes visible as drift.
      "sudo apt-get install -y amdgpu-dkms",
      // ROCm userspace lives in the engine container. Only the SMI tools
      // belong on the host: cloud-init runs `amd-smi list` before joining.
      "sudo apt-get install -y amd-smi-lib rocm-smi-lib rocminfo",
    ]
  }

  // containerd and the kubelet. The node joins at first boot, not here.
  // No container toolkit: see the header.
  provisioner "shell" {
    inline = [
      "set -euxo pipefail",
      "sudo apt-get install -y containerd",
      "curl -fsSL https://pkgs.k8s.io/core:/stable:/v${join(".", slice(split(".", var.kubernetes_version), 0, 2))}/deb/Release.key | sudo gpg --dearmor -o /usr/share/keyrings/kubernetes-apt-keyring.gpg",
      "echo 'deb [signed-by=/usr/share/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v${join(".", slice(split(".", var.kubernetes_version), 0, 2))}/deb/ /' | sudo tee /etc/apt/sources.list.d/kubernetes.list",
      "sudo apt-get update",
      "sudo apt-get install -y kubelet=${var.kubernetes_version}-* kubeadm=${var.kubernetes_version}-*",
      "sudo apt-mark hold kubelet kubeadm",
      "sudo systemctl disable kubelet",
    ]
  }

  // Record what is in the image so fleet health can spot drift. `vendor`
  // is here so a node that was baked from the wrong template is visible
  // as itself rather than as a node whose driver version will not parse.
  provisioner "shell" {
    inline = [
      "set -euxo pipefail",
      "sudo mkdir -p /etc/aisvc",
      "echo '{\"image_version\":\"${var.image_version}\",\"vendor\":\"amd\",\"amdgpu_driver\":\"${var.amdgpu_version}\",\"rocm\":\"${var.rocm_version}\",\"kubernetes\":\"${var.kubernetes_version}\"}' | sudo tee /etc/aisvc/image.json",
    ]
  }

  // Leave nothing behind that would make two nodes from this image identical
  // where they must differ, or that leaks the build host's identity.
  provisioner "shell" {
    inline = [
      "set -euxo pipefail",
      "sudo cloud-init clean --logs --seed",
      "sudo rm -f /etc/machine-id && sudo touch /etc/machine-id",
      "sudo rm -rf /var/lib/cloud/instances/* /etc/ssh/ssh_host_* /root/.ssh/authorized_keys",
      "sudo rm -rf /home/ubuntu/.ssh/authorized_keys /var/log/*.log",
      "sudo apt-get clean",
    ]
  }

  post-processor "manifest" {
    output = "packer-manifest.json"
  }
}
