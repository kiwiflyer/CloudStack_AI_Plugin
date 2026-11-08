// Command terraform-provider-cloudstack-ai serves the AI Services
// resources for Terraform (SPEC.md section 7).
//
// These resources are meant to land in the CloudStack Terraform
// provider upstream; this binary exists so they can be developed and
// acceptance-tested against the simulator before they get there.
package main

import (
	"github.com/hashicorp/terraform-plugin-sdk/v2/plugin"

	"github.com/apache/cloudstack/terraform-provider-cloudstack-ai/internal/provider"
)

func main() {
	plugin.Serve(&plugin.ServeOpts{ProviderFunc: provider.New})
}
