"""A stateful CloudStack stand-in for integration tests.

Unlike the recorded fixtures used by the contract tests, this one changes
when written to: a deployed VM shows up in the next `listVirtualMachines`,
so the reconcile loop can actually converge against it.
"""

from __future__ import annotations

from typing import Any

import httpx

__all__ = ["FakeCloudStack"]

GPUS_PER_HOST = 8

GPU_HOSTS = [
    {
        "id": f"host-{i}",
        "name": f"gpu-h100-{i:02d}",
        "zoneid": "z-1",
        "zonename": "East-1",
        "hosttags": "h100-sxm",
        "type": "Routing",
        "state": "Up",
        "resourcestate": "Enabled",
    }
    for i in range(1, 9)
]


class FakeCloudStack:
    """Scripts CloudStack and keeps the state writes produce."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.vms: list[dict[str, Any]] = []
        self.affinity_groups: list[dict[str, Any]] = []
        self.hosts = [dict(h) for h in GPU_HOSTS]
        self.zones = [{"id": "z-1", "name": "East-1"}]
        self.templates = [{"id": "tpl-1", "name": "gpu-golden-2026.11"}]
        self.offerings = [{"id": "so-1", "name": "GPU-H100-8x"}]
        self.fail_on: str | None = None
        self._next_vm = 1

    # -- inspection -------------------------------------------------------

    def commands(self) -> list[str]:
        return [c for c, _ in self.calls]

    def params_for(self, command: str) -> list[dict[str, Any]]:
        return [p for c, p in self.calls if c == command]

    def gpu_vms(self) -> list[dict[str, Any]]:
        """VMs this platform owns, by naming convention."""
        return [vm for vm in self.vms if vm["name"].startswith("aisvc-")]

    # -- transport --------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        params = dict(httpx.QueryParams(request.url.query.decode()))
        command = params.pop("command")
        for noise in ("apikey", "signature", "response"):
            params.pop(noise, None)
        self.calls.append((command, params))

        if self.fail_on == command:
            return httpx.Response(
                530,
                json={
                    f"{command.lower()}response": {
                        "errorcode": 530,
                        "errortext": "scripted failure",
                    }
                },
            )
        return httpx.Response(200, json={f"{command.lower()}response": self._body(command, params)})

    def _body(self, command: str, params: dict[str, Any]) -> dict[str, Any]:
        match command:
            case "listZones":
                return {"count": len(self.zones), "zone": self.zones}
            case "listHosts":
                return {"count": len(self.hosts), "host": self.hosts}
            case "listGpuDevices":
                devices = self._gpu_devices()
                return {"count": len(devices), "gpudevice": devices}
            case "listTemplates":
                return {"count": len(self.templates), "template": self.templates}
            case "listServiceOfferings":
                return {"count": len(self.offerings), "serviceoffering": self.offerings}
            case "listAffinityGroups":
                return {
                    "count": len(self.affinity_groups),
                    "affinitygroup": self.affinity_groups,
                }
            case "createAffinityGroup":
                group = {
                    "id": f"ag-{len(self.affinity_groups) + 1}",
                    "name": params["name"],
                }
                self.affinity_groups.append(group)
                return {"affinitygroup": group}
            case "listVirtualMachines":
                return {"count": len(self.vms), "virtualmachine": self.vms}
            case "deployVirtualMachine":
                return self._deploy(params)
            case "destroyVirtualMachine":
                self.vms = [v for v in self.vms if v["id"] != params.get("id")]
                return {"jobid": "job-destroy"}
            case "scaleKubernetesCluster":
                return {"jobid": "job-scale"}
            case "queryAsyncJobResult":
                return {"jobstatus": 1, "jobresult": {"ok": True}}
        raise AssertionError(f"unscripted command {command}")

    def _gpu_devices(self) -> list[dict[str, Any]]:
        """Eight passthrough GPUs per host, allocated to whatever runs there.

        Mirrors CloudStack 4.21+ `listGpuDevices`, so capacity is counted
        per GPU rather than inferred from host tags (ADR-0003).
        """
        holder = {vm["hostid"]: vm["id"] for vm in self.vms if vm.get("hostid")}
        devices: list[dict[str, Any]] = []
        for host in self.hosts:
            for index in range(GPUS_PER_HOST):
                device: dict[str, Any] = {
                    "id": f"{host['id']}-gpu-{index}",
                    "hostid": host["id"],
                    "hostname": host["name"],
                    "gpucardid": "card-h100",
                    "gpucardname": "NVIDIA H100 80GB HBM3",
                    "gpudevicetype": "PCI",
                    "busaddress": f"0000:{17 + index:02x}:00.0",
                    "managedstate": "Enabled",
                    "numanode": "0",
                }
                if host["id"] in holder:
                    device["virtualmachineid"] = holder[host["id"]]
                    device["vmstate"] = "Running"
                devices.append(device)
        return devices

    def _deploy(self, params: dict[str, Any]) -> dict[str, Any]:
        """Place the VM on the first host with no aisvc VM already on it."""
        taken = {vm.get("hostid") for vm in self.vms}
        free = next((h for h in self.hosts if h["id"] not in taken), None)
        if free is None:
            return {"errorcode": 533, "errortext": "no capacity"}

        vm = {
            "id": f"vm-{self._next_vm}",
            "name": params["name"],
            "displayname": params["name"],
            "hostid": free["id"],
            "serviceofferingname": "GPU-H100-8x",
            "state": "Running",
            "zoneid": "z-1",
        }
        self._next_vm += 1
        self.vms.append(vm)
        return {"jobid": f"job-{vm['id']}"}
