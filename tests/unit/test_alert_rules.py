"""The shipped Prometheus rules and Grafana dashboard (SPEC.md section 11).

Section 11 names seven things to alert on. These check the shipped rules
still cover them and still reference metrics something actually
produces — a rule pointed at a misspelt series is indistinguishable from
a quiet platform.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

#: These files are data, not code; a precise shape would be noise.
Rules = dict[str, Any]

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "deploy" / "observability" / "prometheus-rules.yaml"
DASHBOARD = ROOT / "deploy" / "observability" / "grafana" / "aisvc-platform.json"

#: Every series the rules are allowed to use, and where it comes from.
KNOWN_METRICS = {
    # vLLM
    "vllm:num_requests_waiting",
    "vllm:time_to_first_token_seconds_bucket",
    # DCGM exporter
    "DCGM_FI_DEV_XID_ERRORS",
    "DCGM_FI_DEV_ECC_SBE_VOL_TOTAL",
    "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL",
    "DCGM_FI_DEV_GPU_UTIL",
    "DCGM_FI_DRIVER_VERSION",
    # The engine itself (aisvc.observability.metrics)
    "aisvc_reconcile_runs_total",
    "aisvc_mirror_requests_total",
}


@pytest.fixture(scope="module")
def rules() -> Rules:
    loaded: Rules = yaml.safe_load(RULES.read_text())
    return loaded


def all_rules(rules: Rules) -> list[Rules]:
    return [rule for group in rules["groups"] for rule in group.get("rules", [])]


class TestTheRulesAreValid:
    def test_the_file_parses(self, rules: Rules) -> None:
        assert rules["groups"]

    def test_every_rule_has_an_expression(self, rules: Rules) -> None:
        assert all(rule.get("expr") for rule in all_rules(rules))

    def test_every_rule_has_a_severity(self, rules: Rules) -> None:
        for rule in all_rules(rules):
            assert rule["labels"]["severity"] in {"warning", "critical"}

    def test_every_rule_says_what_to_do(self, rules: Rules) -> None:
        """An alert an operator cannot act on is a pager that gets muted."""
        for rule in all_rules(rules):
            assert rule["annotations"]["summary"]
            assert rule["annotations"]["runbook"]

    def test_every_rule_references_a_real_metric(self, rules: Rules) -> None:
        """A misspelt series looks exactly like a healthy platform."""
        for rule in all_rules(rules):
            assert any(metric in rule["expr"] for metric in KNOWN_METRICS), (
                f"{rule['alert']} references no known metric: {rule['expr']}"
            )


class TestSectionElevenIsCovered:
    """SPEC.md section 11 names these. Each needs a rule or a reason."""

    @pytest.mark.parametrize(
        "alert",
        [
            "AisvcGpuXidErrors",
            "AisvcGpuEccErrors",
            "AisvcTtftOverSlo",
            "AisvcQueueDepthOverTarget",
            "AisvcGpuDriverDrift",
            "AisvcReconcilerFailing",
            "AisvcMirrorFailing",
        ],
    )
    def test_the_alert_exists(self, rules: Rules, alert: str) -> None:
        assert alert in {rule["alert"] for rule in all_rules(rules)}

    def test_ttft_waits_ten_minutes(self, rules: Rules) -> None:
        """Section 11 says "over SLO for 10 minutes" — firing sooner
        would page on every traffic spike."""
        rule = next(r for r in all_rules(rules) if r["alert"] == "AisvcTtftOverSlo")

        assert rule["for"] == "10m"

    def test_gpu_errors_are_critical(self, rules: Rules) -> None:
        """XID and ECC mean a GPU is failing; that is not a warning."""
        for name in ("AisvcGpuXidErrors", "AisvcGpuEccErrors"):
            rule = next(r for r in all_rules(rules) if r["alert"] == name)
            assert rule["labels"]["severity"] == "critical"

    def test_the_gpu_rules_point_at_drain(self, rules: Rules) -> None:
        """Passthrough GPUs cannot migrate (ADR-0003): the fix is drain
        and replace, and the runbook has to say so."""
        rule = next(r for r in all_rules(rules) if r["alert"] == "AisvcGpuXidErrors")

        assert "drain" in rule["annotations"]["runbook"].lower()

    def test_budget_thresholds_are_accounted_for(self, rules: Rules) -> None:
        """Section 11 lists budget thresholds too, but they are not a
        Prometheus rule: they have to reach the tenant, not the
        operator's pager, so the engine delivers them itself. The file
        has to say so, or the omission reads as a gap."""
        assert "budget" in RULES.read_text().lower()


@pytest.fixture(scope="module")
def dashboard() -> Rules:
    loaded: Rules = json.loads(DASHBOARD.read_text())
    return loaded


class TestTheDashboard:
    def test_it_is_valid_json(self, dashboard: Rules) -> None:
        assert dashboard["title"]

    def test_it_has_panels(self, dashboard: Rules) -> None:
        assert dashboard["panels"]

    def test_every_panel_has_a_title(self, dashboard: Rules) -> None:
        assert all(panel.get("title") for panel in dashboard["panels"])

    def test_every_query_references_a_real_metric(self, dashboard: Rules) -> None:
        for panel in dashboard["panels"]:
            for target in panel.get("targets", []):
                expr = target.get("expr", "")
                assert any(metric in expr for metric in KNOWN_METRICS), (
                    f"panel {panel['title']!r} queries nothing known: {expr}"
                )

    def test_the_things_section_eleven_names_are_on_it(self, dashboard: Rules) -> None:
        titles = " ".join(panel["title"].lower() for panel in dashboard["panels"])
        for subject in ("ttft", "queue", "gpu", "reconcile"):
            assert subject in titles
