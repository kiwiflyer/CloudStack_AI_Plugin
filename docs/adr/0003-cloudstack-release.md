# ADR-0003: Target CloudStack release

**Status:** Accepted
**Date:** 2026-11-07
**Deciders:** Simon Weller
**Resolves:** SPEC.md section 14, "Target CloudStack release, and whether its GPU/vGPU offerings, GPU resource limits and Extensions framework cover part of the plugin"
**Blocks:** Milestone 5 (native API plugin)

## Context

Milestone 5 writes `plugins/ai-services` against a specific CloudStack release's
`PluggableService`, GPU support, resource limits and Extensions framework. Until a release is
pinned, nothing about that plugin can be written.

The release landscape as of November 2026:

| Release | Status | Notes |
| --- | --- | --- |
| 4.21 | superseded | Introduced native GPU support and the Extensions framework |
| 4.22 (4.22.1.1) | **current LTS** | EOL around December 2027 |
| **4.23.0.0** | **current, non-LTS "Regular"** | Networking, security and storage; supported only until the next regular or LTS release |
| 4.24 | planned **LTS** | Per the release calendar |

## Decision

**Target Apache CloudStack 4.23.0.0.**

Hold the plugin's compile-time surface to APIs that also exist in **4.22 LTS**, so that moving
to 4.24 LTS for GA costs a rebuild rather than a rewrite. Where a 4.23-only API is genuinely
needed, isolate it behind an interface and record it here.

## The release is non-LTS, and that matters

4.23 is a Regular release. The project supports it **only until the next regular or LTS
release**, which is a materially shorter window than the 18–24 months an LTS gets. Providers
— the people who install this platform — overwhelmingly run LTS.

So the practical risk is not that 4.23 is unsuitable for development; it is that **a product
built and tested only against 4.23 cannot be installed by customers who run LTS**, and that
4.23 will be out of support before this platform reaches v1 GA at the current milestone pace.

This is recorded as a stated factor rather than an objection. The mitigation above — keeping
to the 4.22 LTS API surface and expecting to retarget 4.24 LTS — makes the cost of the
decision small, and 4.23 is the right thing to develop against in the meantime: it is current,
it has every GPU feature we rely on, and its NetworkOrchestrator extensions are the obvious
substrate for v2 private endpoints.

**Revisit when 4.24 LTS ships**, and plan to certify against it for GA.

## What 4.23 gives us, and what it does not

SPEC.md section 14 asked specifically whether three things cover part of the plugin.

### GPU and vGPU offerings — covered, use them

Native GPU support arrived in **4.21**, not 4.23, so it is well settled by now:

- GPU devices are **discovered automatically** when a host connects to the management server.
- **GPU-enabled compute offerings** select a card and profile; instances deployed with that
  offering get the GPU.
- **Passthrough and vGPU** are both supported, each counting as one device.

This is exactly the model `gpuClasses[].service_offering` already assumes, and milestone 2's
`NodeProvisioner` already deploys against a service offering. No change needed.

### GPU resource limits — covered, do not rebuild

`max.account.gpus`, `max.domain.gpus` and `max.project.gpus` are global settings, overridable
per account, domain and project. SPEC.md section 9 says to use "native GPU resource limits
where the target release supports them" — 4.23 supports them, so the plugin configures them
rather than implementing a parallel quota system.

### Extensions framework — **does not** cover this plugin

SPEC.md section 9 says: "Before writing bespoke Java, evaluate whether the Extensions
framework in the target release covers any custom actions." Evaluated; it does not.

4.23 has two extension types, **Orchestrator** (registered with clusters) and
**NetworkOrchestrator** (registered with physical networks). Custom actions can target
`VirtualMachine` for the first, and `Network` and `Vpc` for the second. There is **no way to
define a new first-class resource type with its own API commands.**

Our commands create and manage AI deployments, models, profiles, keys and budgets — new
resources, not actions on existing VMs or networks. Extensions cannot express them, so the
Java API plugin of milestone 5 is necessary rather than merely conventional. That question is
now closed.

The framework stays interesting for **v2 private endpoints** (section 8), where attaching an
endpoint to a tenant VPC tier is exactly a custom action on a `Vpc`.

## Consequences for code that already exists

Two findings change decisions made in earlier milestones.

### GPU device discovery should replace host-tag inference

`aisvc/inventory` counts GPU capacity by matching `gpuClasses[].host_tag` against host tags,
and counts a host as allocated when it runs a VM with the class's service offering. PROGRESS
recorded the limitation: *"Node allocation is counted per host, not per GPU. That holds for
passthrough; vgpu and mig classes will need per-GPU accounting."*

4.21+ reports actual GPU devices per host. That is a better source of truth and it resolves
the open question: per-GPU accounting comes from CloudStack rather than from us.

**Done.** `inventory` now reads `listGpuDevices`, counting allocation from each device's
`virtualmachineid`. A physical GPU carved into vGPUs is treated as a container rather than a
unit, so parent and children are never double-counted, and `free_by_host` is tracked
alongside `allocated_by_host` because a vGPU host is partly allocated and partly free at the
same time — something subtracting one from a total cannot express. `host_tag` now selects
hosts; `gpus_per_node` bounds one node VM and `tensor_parallel`, not a host's physical GPU
count.

**Amended 2026-11-18, twice, by a real 4.23.0.0 — a fake had agreed with both
readings.** CloudStack reports *two* fields about a GPU and this read one.
`GpuDevice.usable` tested `managedstate == "Enabled"`, a value 4.23.0.0 never
returns: it reports `Managed` or `Unmanaged`, so every real GPU read as unusable
and a platform full of idle cards would have refused every deployment. The check
is a deny-list now, because an allow-list that misses a value fails silently
everywhere while a deny-list that misses one fails loudly in one place. Then
`state` turned out never to be parsed at all: on the live zone all 112 devices
were `managedstate=Managed` while sixteen of them — every H100, the only
inference-capable capacity there — carried `state=Error`, so the engine counted
them as free. `GpuDevice.State` is `Allocated`, `Free`, `Error` or
`PartiallyAllocated` (read from the management server's source, the API
reference being wrong about the field beside it); only `Error` means "not
capacity". Absence is read differently for the two on purpose: a missing
`managedstate` leaves the primary question unanswered and is distrusted, while a
missing `state` reports no fault, because treating silence as a fault would make
any server that trims the field a zero-capacity platform — which is exactly how
the `managedstate` bug failed. **Whenever CloudStack reports two fields about one
thing, read both.**

### Stopping a GPU node does not release its GPU

`gpu.detach.on.stop` defaults to false, so a stopped instance keeps its GPU allocated. Our
`destroy_nodes` expunges rather than stopping, so milestone 2 is correct as written — but
milestone 7's drain must **not** be implemented as "stop the VM", or drained nodes will hold
GPUs they are not using. Worth a test when `lifecycle` lands.

### Other constraints worth carrying forward

- **KVM only.** Native GPU support is KVM-only, so the hypervisor is pinned by implication.
- **Validated on NVIDIA A10.** Our examples assume H100 and L40S; those are not what upstream
  validated against, so the GPU lab is the only place that claim gets tested.
- **Hosts need IOMMU on and Nouveau disabled** before passthrough works. That belongs in the
  setup wizard's preflight checks (milestone 8), not left for a failed deployment to reveal.
- **Kubernetes GPU scheduling needs the NVIDIA GPU Operator**, separately from the host
  driver. `deploy/` already lists GPU Operator values; this confirms it is required, not
  optional.

## Consequences

**Easier**
- Milestone 5 can start: the `PluggableService` surface, ConfigKeys and resource-limit APIs
  are all pinned to known versions.
- GPU offerings and limits are CloudStack's job, not ours, which removes a quota system we
  would otherwise have had to build and reconcile.
- The Extensions question is settled, so no further time goes into evaluating it.

**Harder**
- Tracking a non-LTS release means a planned retarget to 4.24 LTS before GA, and discipline
  about not reaching for 4.23-only APIs in the meantime.
- `inventory` has a known mis-count for vGPU and MIG until it moves to the GPU device APIs.

**To revisit**
- When 4.24 LTS ships: retarget, and certify against it for GA.
- If a 4.23-only API turns out to be unavoidable, record it here and accept that 4.22 LTS is
  then out of reach.

## Action items

1. [x] **Done, and the mitigation holds.** `java-plugin/` builds against 4.23.0.0, and all
       361 tests also pass against **4.22.1.1 LTS** (`mvn -Dcs.version=4.22.1.1 test`) — so
       nothing 4.23-only has crept in. A CI job runs the LTS build on every change, so a
       4.23-only dependency fails there rather than at the GA retarget.
2. [ ] Configure `max.account.gpus` / `max.domain.gpus` from the plugin rather than
       implementing GPU quotas in the engine.
3. [x] **Done.** `aisvc/inventory` reads `listGpuDevices`; `host_tag` selects hosts and
       `gpus_per_node` bounds one node VM, while capacity is counted per device.
       `gpuClasses[].gpu_card` narrows counting on mixed-GPU hosts.
4. [~] `aisvc/lifecycle.plan_drain` destroys rather than stops, asserted by a test naming
       this ADR. That the GPU is *actually* released still needs the GPU lab.
5. [ ] Milestone 8: add IOMMU-enabled and Nouveau-disabled to the setup wizard's host checks.
6. [ ] Re-evaluate the Extensions framework for v2 private endpoints, where a `Vpc` custom
       action is the right shape.
7. [ ] Plan the 4.24 LTS retarget as a GA gate.

## Sources

- [Apache CloudStack Downloads](https://cloudstack.apache.org/downloads/) — 4.23.0.0 current, 4.22.1.1 current LTS
- [Apache CloudStack 4.23.0.0 Release](https://cloudstack.apache.org/blog/cloudstack-4.23.0.0-release/) — announced as a non-LTS release
- [LTS, Apache CloudStack wiki](https://cwiki.apache.org/confluence/display/CLOUDSTACK/LTS) — Regular releases supported only to the next release; 4.24 planned as LTS
- [Apache CloudStack 4.23 release overview](https://www.shapeblue.com/apache-cloudstack-4-23-release-overview/) — networking, security and storage focus
- [Apache CloudStack GPU Support](https://www.shapeblue.com/apache-cloudstack-gpu-support/) — GPU support in 4.21: discovery, offerings, passthrough/vGPU, `max.*.gpus`, `gpu.detach.on.stop`, KVM only, A10-validated, IOMMU and Nouveau prerequisites
- [Extensions, CloudStack 4.23 docs](https://docs.cloudstack.apache.org/en/latest/adminguide/extensions.html) — Orchestrator and NetworkOrchestrator types; custom actions on VirtualMachine, Network and Vpc only
- [Extending CloudStack with the Extensions Framework](https://www.shapeblue.com/extending-apache-cloudstack-with-the-extensions-framework/) — framework introduced in 4.21
