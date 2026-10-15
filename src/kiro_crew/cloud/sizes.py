"""Instance size tiers for the cloud launcher (constants — no magic numbers).

Kiro Crew uses ~10 GB RAM with spikes beyond that (see
``docs/guides/remote-and-mobile.md``), so every tier is **≥16 GB RAM** — 8 GB is
below the working set and gets OOM-killed under load, so it is not offered.

The tiers are laddered by **how much runs at once**, not by GB: the sub-agent
concurrency cap is CPU-bound (``subagent.py`` derives it from vCPU), so RAM alone
does not raise it. Effective parallel sub-agents per tier:

* **Light** — 16 GB / 4 vCPU  → ~3 sub-agents (the floor); single-threaded work.
* **Development** (default, recommended) — 32 GB / 8 vCPU → ~6 sub-agents; the
  normal working setup.
* **Power** — 64 GB / **16 vCPU** → ~12 sub-agents; large fan-outs, long builds.
  (The vCPU count is what matters — a 64 GB box with only 8 vCPU would still cap
  at ~6, so the Power tier is ``m7g.4xlarge`` / 16 vCPU, not a memory-optimized
  shape.)

We default to **arm64 / Graviton** (cheaper per GB; both Kiro Crew and ``kiro-cli``
ship aarch64 Linux builds), with an x86_64 lane for users who need it.

Prices are illustrative on-demand USD/hour and are surfaced only as "approximate"
in the CLI — never used for anything but display, and the dashboard shows no
dollar figure at all (it links the AWS Pricing Calculator instead).
"""

from __future__ import annotations

from dataclasses import dataclass

# CloudFormation architecture parameter values (also select the AMI alias).
ARCH_ARM64 = "arm64"
ARCH_X86_64 = "x86_64"


@dataclass(frozen=True)
class SizeTier:
    """One selectable instance size."""

    key: str  # stable id used on the CLI (--size) and in config
    label: str  # short human label
    instance_type: str  # EC2 instance type
    arch: str  # ARCH_ARM64 | ARCH_X86_64
    vcpu: int
    ram_gb: int
    disk_gb: int  # gp3 root volume size
    approx_usd_per_hr: float  # illustrative on-demand price
    recommended: bool = False

    def summary(self) -> str:
        """One-line human summary for menus and confirmations."""
        arch = "arm64" if self.arch == ARCH_ARM64 else "x86_64"
        return (
            f"{self.instance_type}  {arch} · {self.ram_gb} GB · {self.vcpu} vCPU · "
            f"{self.disk_gb} GB disk  ~${self.approx_usd_per_hr:.2f}/hr"
        )


# arm64 / Graviton tiers (the default lane). Light is burstable t4g (cheap at
# idle, bursts for tool calls); Development/Power use non-burstable m7g for
# sustained parallel load. Keys (light/balanced/power) are stable CLI --size ids
# — the ladder was raised (8 GB retired) but the keys did not change, so existing
# --size values and saved configs keep resolving.
_TIERS: tuple[SizeTier, ...] = (
    SizeTier(
        key="light",
        label="Light",
        instance_type="t4g.xlarge",
        arch=ARCH_ARM64,
        vcpu=4,
        ram_gb=16,
        disk_gb=40,
        approx_usd_per_hr=0.134,
    ),
    SizeTier(
        key="balanced",
        label="Development",
        instance_type="m7g.2xlarge",
        arch=ARCH_ARM64,
        vcpu=8,
        ram_gb=32,
        disk_gb=60,
        approx_usd_per_hr=0.326,
        recommended=True,
    ),
    SizeTier(
        key="power",
        label="Power",
        instance_type="m7g.4xlarge",
        arch=ARCH_ARM64,
        vcpu=16,
        ram_gb=64,
        disk_gb=80,
        approx_usd_per_hr=0.653,
    ),
    # x86_64 lane — same shapes, for users who need Intel/AMD.
    SizeTier(
        key="light-x86",
        label="Light (x86_64)",
        instance_type="t3.xlarge",
        arch=ARCH_X86_64,
        vcpu=4,
        ram_gb=16,
        disk_gb=40,
        approx_usd_per_hr=0.166,
    ),
    SizeTier(
        key="balanced-x86",
        label="Development (x86_64)",
        instance_type="m7i.2xlarge",
        arch=ARCH_X86_64,
        vcpu=8,
        ram_gb=32,
        disk_gb=60,
        approx_usd_per_hr=0.403,
    ),
    SizeTier(
        key="power-x86",
        label="Power (x86_64)",
        instance_type="m7i.4xlarge",
        arch=ARCH_X86_64,
        vcpu=16,
        ram_gb=64,
        disk_gb=80,
        approx_usd_per_hr=0.806,
    ),
)

TIERS_BY_KEY: dict[str, SizeTier] = {t.key: t for t in _TIERS}

DEFAULT_TIER_KEY = "balanced"

# The tiers offered in the interactive picker, in display order (the x86 lane is
# reachable via --size but kept out of the default 3-choice menu for simplicity).
INTERACTIVE_TIER_KEYS = ("light", "balanced", "power")


def all_tiers() -> tuple[SizeTier, ...]:
    """All defined tiers (arm64 lane first, then x86)."""
    return _TIERS


def interactive_tiers() -> list[SizeTier]:
    """The tiers shown in the wizard's size picker."""
    return [TIERS_BY_KEY[k] for k in INTERACTIVE_TIER_KEYS]


def get_tier(key: str) -> SizeTier:
    """Resolve a tier by key; raise ``KeyError`` with the valid set if unknown."""
    try:
        return TIERS_BY_KEY[key]
    except KeyError:
        valid = ", ".join(TIERS_BY_KEY)
        raise KeyError(f"unknown size '{key}' (choose one of: {valid})") from None


def default_tier() -> SizeTier:
    """The recommended default tier."""
    return TIERS_BY_KEY[DEFAULT_TIER_KEY]


def monthly_estimate(tier: SizeTier, hours_per_day: float = 24.0) -> float:
    """Approximate USD/month for a tier at the given daily uptime (display only)."""
    return round(tier.approx_usd_per_hr * hours_per_day * 30, 2)


# --- Lightsail bundle mapping ------------------------------------------------
# The Lightsail lane reuses the SAME size keys (light/balanced/power), so the
# user picks a tier identically — only the compute underneath differs. Lightsail
# bundles are FIXED monthly price with bandwidth and SSD included, so unlike the
# EC2 `approx_usd_per_hr` (illustrative only, display-suppressed on the
# dashboard) the Lightsail monthly figure is a REAL number the UI can show.
#
# Kept as a separate catalog rather than a field on SizeTier so the EC2
# `instance_type` stays EC2-specific and the x86 lane (which Lightsail does not
# mirror) needs no touching.
#
# The 16/32/64 GB Linux general-purpose bundles map 1:1 to the EC2 tiers' vCPU
# counts (16 GB=4 vCPU, 32 GB=8 vCPU, 64 GB=16 vCPU), so there is no RAM-floor
# gap. The bundle *ids* here are the current public ``*_3_0`` general-purpose
# ids; a launch resolves the exact available id for the chosen region with
# ``aws lightsail get-bundles`` (the engine falls back to these when it cannot).


@dataclass(frozen=True)
class LightsailBundle:
    """A Lightsail bundle a size tier maps to (the fixed-price-plan analog of an
    EC2 instance type)."""

    key: str  # the SAME tier key the EC2 lane uses (light/balanced/power)
    bundle_id: str  # Lightsail bundle id, e.g. "large_3_0"
    vcpu: int
    ram_gb: int
    disk_gb: int  # bundle SSD, included in the price
    transfer_tb: int  # monthly data transfer, included in the price
    usd_per_month: float  # FIXED monthly price (real, not illustrative)

    def summary(self) -> str:
        """One-line human summary for menus and confirmations."""
        return (
            f"{self.ram_gb} GB · {self.vcpu} vCPU · {self.disk_gb} GB SSD · "
            f"{self.transfer_tb} TB transfer  ${self.usd_per_month:.0f}/mo"
        )


# Keyed by the same tier keys as the EC2 ladder. Only the three default-menu
# tiers are mapped (Lightsail has no separate x86 lane); the specs match the EC2
# tiers' vCPU exactly, so the picker is identical and the mapping is 1:1.
_LIGHTSAIL_BUNDLES: tuple[LightsailBundle, ...] = (
    LightsailBundle(
        key="light",
        bundle_id="large_3_0",
        vcpu=4,
        ram_gb=16,
        disk_gb=320,
        transfer_tb=6,
        usd_per_month=84.0,
    ),
    LightsailBundle(
        key="balanced",
        bundle_id="xlarge_3_0",
        vcpu=8,
        ram_gb=32,
        disk_gb=640,
        transfer_tb=7,
        usd_per_month=164.0,
    ),
    LightsailBundle(
        key="power",
        bundle_id="2xlarge_3_0",
        vcpu=16,
        ram_gb=64,
        disk_gb=1280,
        transfer_tb=8,
        usd_per_month=384.0,
    ),
)

LIGHTSAIL_BUNDLES_BY_KEY: dict[str, LightsailBundle] = {b.key: b for b in _LIGHTSAIL_BUNDLES}

# The Lightsail lane offers the same interactive keys as EC2.
LIGHTSAIL_INTERACTIVE_KEYS = ("light", "balanced", "power")


def lightsail_bundle(key: str) -> LightsailBundle:
    """Resolve a Lightsail bundle by tier key; raise ``KeyError`` with the valid set.

    A key the EC2 ladder has but Lightsail does not map (the x86 lane) raises,
    naming the Lightsail keys — so a ``--size light-x86 --provider lightsail`` is
    refused with an actionable message rather than silently falling back.
    """
    try:
        return LIGHTSAIL_BUNDLES_BY_KEY[key]
    except KeyError:
        valid = ", ".join(LIGHTSAIL_BUNDLES_BY_KEY)
        raise KeyError(
            f"size '{key}' has no Lightsail bundle (choose one of: {valid})"
        ) from None


def lightsail_bundles() -> tuple[LightsailBundle, ...]:
    """All Lightsail bundles, in display order (light → balanced → power)."""
    return _LIGHTSAIL_BUNDLES
