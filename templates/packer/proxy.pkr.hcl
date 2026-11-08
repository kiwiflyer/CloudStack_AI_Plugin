// Per-tenant gateway proxy template (SPEC.md section 8).
//
// A private endpoint is a small VM with a NIC on the tenant's VPC tier,
// forwarding to the in-cluster gateway. It is a VM and not a Kubernetes
// LoadBalancer Service because CloudStack's cloud-controller-manager
// takes a service's network from the nodes' first NIC and offers no way
// to choose one — see docs and PROGRESS.md.
//
// Almost nothing is baked in beyond nginx and aisvc-fetch-cert: the
// per-endpoint configuration arrives as cloud-init, rendered by
// aisvc/endpoints/proxy.py. The one thing that must be in the image is
// the certificate fetcher, because userdata is readable through the
// CloudStack API and a private key must never travel in it.

packer {
  required_plugins {
    cloudstack = {
      version = ">= 1.1.0"
      source  = "github.com/hashicorp/cloudstack"
    }
  }
}

variable "image_version" {
  type        = string
  description = "Template name, e.g. aisvc-proxy-2026.11. Referenced by zones[].proxy_template."
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
  description = "A small CPU offering; the proxy terminates TLS and forwards."
  default     = "Small-Instance"
}

variable "network" {
  type        = string
  description = "Network with egress, for package downloads during the build."
}

source "cloudstack" "proxy" {
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
  template_display_text = "CloudStack AI Services gateway proxy ${var.image_version}"
  template_os           = "Ubuntu 24.04 LTS"
  template_featured     = false
  template_public       = false
}

build {
  sources = ["source.cloudstack.proxy"]

  provisioner "file" {
    source      = "files/aisvc-fetch-cert"
    destination = "/tmp/aisvc-fetch-cert"
  }

  provisioner "shell" {
    inline = [
      "set -euxo pipefail",
      "export DEBIAN_FRONTEND=noninteractive",
      "sudo apt-get update",
      // certbot is installed here rather than by cloud-init so an ACME
      // endpoint does not wait on a package download at first boot.
      "sudo apt-get install -y nginx certbot python3-certbot-nginx curl ca-certificates",

      "sudo install -m 0755 /tmp/aisvc-fetch-cert /usr/local/bin/aisvc-fetch-cert",
      "sudo mkdir -p /etc/aisvc/tls /etc/aisvc/mtls",
      "sudo chmod 0700 /etc/aisvc/tls",

      // The proxy serves one deployment and nothing else; the default
      // site would answer on the tenant's network with an nginx page.
      "sudo rm -f /etc/nginx/sites-enabled/default",

      // Recorded so fleet health can compare a running proxy against
      // the image it came from, as GPU nodes do.
      "echo '{\"image\": \"${var.image_version}\", \"role\": \"gateway-proxy\"}' | sudo tee /etc/aisvc/image.json",

      "sudo systemctl enable nginx",
    ]
  }
}
