// Golden GPU node template (SPEC.md sections 7 and 13).
//
// Everything slow or network-dependent is baked in here so cloud-init on a
// new node only has to identify itself and join: SPEC.md section 11 targets
// a cold start under three minutes, which is not reachable if a node has to
// install a driver first. Fleet health (section 7) compares running nodes
// against the driver and toolkit versions recorded in /etc/aisvc/image.json.

packer {
  required_plugins {
    cloudstack = {
      # The plugin's newest release is 1.0.2; ">= 1.1.0" resolved to
      # nothing and `packer init` refused the template outright.
      version = ">= 1.0.2"
      source  = "github.com/hashicorp/cloudstack"
    }
  }
}

variable "image_version" {
  type        = string
  description = "Template name, e.g. gpu-golden-2026.11. Referenced by nodePools[].template."
}

variable "nvidia_driver_version" {
  type    = string
  default = "570.86.15"
}

variable "container_toolkit_version" {
  type    = string
  default = "1.17.3"
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
  template_display_text = "CloudStack AI Services GPU node ${var.image_version}"
  template_os           = "Ubuntu 24.04 LTS"
  template_featured     = false
  template_public       = false
  template_scalable     = true
}

build {
  name    = "aisvc-gpu-node"
  sources = ["source.cloudstack.gpu"]

  provisioner "shell" {
    inline = [
      "set -euxo pipefail",
      "cloud-init status --wait || true",
      "sudo apt-get update",
      "sudo apt-get install -y --no-install-recommends ca-certificates curl gnupg jq nvme-cli",
    ]
  }

  // NVIDIA driver and container toolkit.
  provisioner "shell" {
    inline = [
      "set -euxo pipefail",
      "curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg",
      "curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list",
      "sudo apt-get update",
      "sudo apt-get install -y nvidia-headless-${split(".", var.nvidia_driver_version)[0]}-server nvidia-utils-${split(".", var.nvidia_driver_version)[0]}-server",
      "sudo apt-get install -y nvidia-container-toolkit=${var.container_toolkit_version}-1",
    ]
  }

  // containerd and the kubelet. The node joins at first boot, not here.
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

  // Record what is in the image so fleet health can spot drift.
  provisioner "shell" {
    inline = [
      "set -euxo pipefail",
      "sudo mkdir -p /etc/aisvc",
      "echo '{\"image_version\":\"${var.image_version}\",\"nvidia_driver\":\"${var.nvidia_driver_version}\",\"container_toolkit\":\"${var.container_toolkit_version}\",\"kubernetes\":\"${var.kubernetes_version}\"}' | sudo tee /etc/aisvc/image.json",
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
