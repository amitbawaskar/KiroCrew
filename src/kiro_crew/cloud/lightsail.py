"""Lightsail lifecycle for the cloud launcher — provision / status / stop / start / destroy.

The Lightsail analog of :mod:`kiro_crew.cloud.ec2`. Every AWS interaction goes
through the single ``aws`` CLI chokepoint in :mod:`kiro_crew.cloud.aws`, exactly
like the EC2 lane; there is no ``boto3`` and no direct subprocess.

**Why Lightsail is a sibling of EC2, not a reuse of it.** The EC2 lane provisions
a CloudFormation *stack* (instance + IAM role + security group + EBS volume) and
tears it down with one ``delete-stack``. Lightsail has none of that machinery: an
instance is created with a single ``create-instances`` call against a *bundle*
(fixed-price plan) and a *blueprint* (base image), its firewall is a per-instance
``put-instance-public-ports`` call, and there is no stack to delete — teardown is
``delete-instance``. So this is a parallel lane with the same five-verb surface
(``deploy``/``status``/``stop``/``start``/``destroy``) rather than a parameterised
EC2.

**Blueprint, not bootstrap.** The lane launches the published Kiro Crew Lightsail
blueprint (:data:`KIRO_CREW_BLUEPRINT_ID`) and nothing else. The image already
carries Kiro Crew and kiro-cli and starts the gateway as a ``kirocrew`` systemd
service on first boot, so no user-data is sent. Lightsail has no WaitCondition,
so the launch engine (:mod:`kiro_crew.cloud.lightsail_engine`) polls the instance
over SSH until the service is active.

Discovery is **stateless-by-tag**, like the EC2 lane: every instance carries the
``kirocrew:managed=true`` + ``kirocrew:instance=<tag>`` tags (Lightsail tags), and
the instance name is deterministic (``kirocrew-<tag>``), so
``status``/``stop``/``start``/``destroy`` find the right instance without a local
cache that could drift.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from kiro_crew.cloud import aws, sizes
from kiro_crew.deploy import profiles as profiles_mod
from kiro_crew.validation import FieldSpec, ValidationError, validate_field

logger = logging.getLogger(__name__)

# Physical instance naming: kirocrew-<tag>. The tag is charset-validated.
INSTANCE_PREFIX = "kirocrew-"
MANAGED_TAG_KEY = "kirocrew:managed"
MANAGED_TAG_VALUE = "true"
INSTANCE_TAG_KEY = "kirocrew:instance"

# Lightsail firewall: SSH only. The gateway is loopback-only on the box and
# reached by an SSH tunnel (see lightsail_engine / connect), exactly as the EC2
# lane keeps its security group with no inbound dashboard port.
SSH_PORT = 22

# The per-call aws timeout is small (these are quick control-plane calls); the
# wait for the gateway to come up is the launch engine's poll loop, not a single
# CLI call.
_POLL_TIMEOUT = 60
# How long to wait for `create-instances` to settle to a `running` instance
# BEFORE the engine begins its gateway health poll. The box reaching `running`
# is a quick Lightsail control-plane transition; the slower first-boot start of
# the gateway is gated separately by the engine.
_RUNNING_TIMEOUT = 300
_RUNNING_POLL_SECS = 5

# --- input validation (values flow into subprocess argv) -------------------
# Lightsail instance names allow letters, digits, hyphen, underscore and period;
# the launcher only ever composes `kirocrew-<tag>`, so cap the tag so the whole
# name stays well within Lightsail's 254-char limit and shares the EC2 lane's
# conservative 51-char charset.
_TAG_RE = re.compile(r"^[a-zA-Z0-9-]{1,51}\Z")
_TAG_SPEC = FieldSpec(name="tag", type=str, max_len=51, pattern=_TAG_RE)
_REGION_RE = re.compile(r"^[a-z]{2}-[a-z]+-\d+\Z")
_REGION_SPEC = FieldSpec(name="region", type=str, max_len=32, pattern=_REGION_RE)
# Aliased from deploy/profiles.py exactly as the EC2 lane does.
_PROFILE_SPEC = profiles_mod.PROFILE_SPEC
# Lightsail bundle ids look like ``large_3_0`` / ``xlarge_3_0``.
_BUNDLE_RE = re.compile(r"^[a-zA-Z0-9_]{1,64}\Z")
_BUNDLE_SPEC = FieldSpec(name="bundle_id", type=str, max_len=64, pattern=_BUNDLE_RE)

#: The published Kiro Crew Lightsail blueprint: Ubuntu 24 with Kiro Crew and
#: kiro-cli preinstalled, the gateway running as the ``kirocrew`` service account,
#: and ``ubuntu`` as the SSH login. Every launch uses it.
KIRO_CREW_BLUEPRINT_ID = "kiro_crew_ls_1_0"


def validate_profile(profile: str) -> str:
    return validate_field(profile, _PROFILE_SPEC) or ""


def validate_region(region: str) -> str:
    return validate_field(region, _REGION_SPEC) or ""


def validate_tag(tag: str) -> str:
    val = validate_field(tag, _TAG_SPEC)
    if not val:
        raise ValidationError("tag", "required")
    return val


def validate_bundle_id(bundle_id: str) -> str:
    return validate_field(bundle_id, _BUNDLE_SPEC) or ""


def validate_ssh_cidr(cidr: str) -> str:
    """Validate and normalize an SSH source CIDR; "" means "any address".

    Reuses the EC2 lane's rules (IPv4 only, /16 or narrower, host bits cleared)
    so both providers accept and refuse the same values.
    """
    if not cidr:
        return ""
    from kiro_crew.cloud import ec2

    return ec2._validate_cidr(cidr)


def instance_name(tag: str) -> str:
    """Deterministic Lightsail instance name for a discovery tag."""
    return f"{INSTANCE_PREFIX}{tag}"


@dataclass
class DeployResult:
    """Outcome of a Lightsail launch (mirrors ec2.DeployResult's shape)."""

    tag: str
    instance_name: str
    region: str
    instance_id: str = ""  # the Lightsail instance name (its stable id for us)
    public_ip: str = ""
    state: str = ""
    bundle_id: str = ""
    blueprint_id: str = ""
    reused: bool = False
    dry_run: bool = False
    argv: list[str] = field(default_factory=list)


def build_deploy_argv(
    *,
    tag: str,
    availability_zone: str,
    bundle_id: str,
) -> list[str]:
    """Assemble the exact ``aws lightsail create-instances`` argv (also the dry-run output).

    Always launches :data:`KIRO_CREW_BLUEPRINT_ID`, with no ``--user-data`` (the
    image starts the gateway itself). Tags the instance ``kirocrew:managed=true``
    + ``kirocrew:instance=<tag>`` so discovery is stateless-by-tag, mirroring the
    EC2 lane.
    """
    return [
        "lightsail",
        "create-instances",
        "--instance-names",
        instance_name(tag),
        "--availability-zone",
        availability_zone,
        "--blueprint-id",
        KIRO_CREW_BLUEPRINT_ID,
        "--bundle-id",
        bundle_id,
        "--tags",
        f"key={MANAGED_TAG_KEY},value={MANAGED_TAG_VALUE}",
        f"key={INSTANCE_TAG_KEY},value={tag}",
    ]


@dataclass(frozen=True)
class ResolvedBundle:
    """The Lightsail bundle a size tier maps to in one region."""

    bundle_id: str
    usd_per_month: float
    live: bool  # True when read from get-bundles, False when it is the catalog fallback


# Standard (not compute/memory-optimized, not Windows, not IPv6-only) bundle ids
# look like ``xlarge_3_0``; the trailing number differs by region (``xlarge_3_2``
# in ap-southeast-2, ``xlarge_3_1`` in ap-south-1 and sa-east-1).
_STANDARD_BUNDLE_RE = re.compile(r"^[0-9]*xlarge_3_[0-9]+\Z")


def resolve_bundle(key: str, profile: str = "", region: str = "") -> ResolvedBundle:
    """Resolve a size tier to the bundle that matches its specs in ``region``.

    Bundle ids and prices are per region, so the catalog id in :mod:`sizes` is only
    a fallback. This reads ``get-bundles`` and picks the active Linux standard
    bundle with the tier's vCPU and RAM; when the call fails or nothing matches it
    returns the catalog bundle and ``live=False`` so the caller can say the price
    is approximate.
    """
    catalog = sizes.lightsail_bundle(key)
    fallback = ResolvedBundle(catalog.bundle_id, catalog.usd_per_month, live=False)
    profile = validate_profile(profile)
    region = validate_region(region)
    try:
        data = aws.checked_json(
            ["lightsail", "get-bundles"], profile, region, action="lightsail:GetBundles"
        )
    except aws.AWSError as exc:
        logger.info("get-bundles failed in %s (%s); using the catalog bundle", region, exc)
        return fallback
    bundles = (data or {}).get("bundles", []) if isinstance(data, dict) else []
    matches = [
        b
        for b in bundles
        if isinstance(b, dict)
        and b.get("isActive", True)
        and "LINUX_UNIX" in (b.get("supportedPlatforms") or [])
        and _STANDARD_BUNDLE_RE.match(str(b.get("bundleId", "")))
        and b.get("cpuCount") == catalog.vcpu
        and float(b.get("ramSizeInGb", 0)) == float(catalog.ram_gb)
    ]
    if not matches:
        return fallback
    best = min(matches, key=lambda b: float(b.get("price", 0)))
    return ResolvedBundle(str(best["bundleId"]), float(best.get("price", 0)), live=True)


def blueprint_available(profile: str = "", region: str = "") -> bool:
    """Whether :data:`KIRO_CREW_BLUEPRINT_ID` exists in ``region``.

    Read from ``get-blueprints`` so an unsupported region fails before anything is
    created, with a clear message, instead of at ``create-instances``.
    """
    profile = validate_profile(profile)
    region = validate_region(region)
    data = aws.checked_json(
        ["lightsail", "get-blueprints", "--include-inactive"],
        profile,
        region,
        action="lightsail:GetBlueprints",
    )
    blueprints = (data or {}).get("blueprints", []) if isinstance(data, dict) else []
    return any(isinstance(b, dict) and b.get("blueprintId") == KIRO_CREW_BLUEPRINT_ID for b in blueprints)


def default_availability_zone(region: str) -> str:
    """A deterministic AZ for ``create-instances`` (``<region>a``).

    Lightsail requires an availability zone, not just a region. ``<region>a``
    exists in every current Lightsail region and keeps the launch non-interactive;
    an operator who needs a specific AZ passes it explicitly to ``deploy``.
    """
    return f"{region}a"


def deploy(
    *,
    tag: str,
    tier: sizes.SizeTier,
    bundle_id: str,
    profile: str = "",
    region: str = "",
    availability_zone: str = "",
    ssh_cidr: str = "",
    dry_run: bool = False,
) -> DeployResult:
    """Provision a Lightsail instance for the given tier. Idempotent by instance name.

    ``tier`` carries the abstract size (``light``/``balanced``/``power``) for
    display/consistency with the EC2 lane; ``bundle_id`` is the concrete Lightsail
    bundle the size resolves to in ``region`` (see :func:`resolve_bundle`).

    The instance is always created from :data:`KIRO_CREW_BLUEPRINT_ID`.

    ``ssh_cidr`` restricts the SSH rule to that IPv4 CIDR (and closes the IPv6
    rule). Empty leaves SSH open to any address. The firewall is only set on a
    NEW instance: ``put-instance-public-ports`` replaces every rule, so applying
    it to an instance that already exists would wipe ports the customer opened
    and reset a restricted SSH rule to "any address".

    ``dry_run`` returns the exact argv without calling AWS.
    """
    if not dry_run:
        aws.assert_human_action("lightsail:CreateInstances")
    tag = validate_tag(tag)
    ssh_cidr = validate_ssh_cidr(ssh_cidr)
    profile = validate_profile(profile)
    region = validate_region(region)
    bundle_id = validate_bundle_id(bundle_id)
    if not bundle_id:
        raise ValidationError("bundle_id", "required")
    az = availability_zone or default_availability_zone(region)

    argv = build_deploy_argv(tag=tag, availability_zone=az, bundle_id=bundle_id)

    if dry_run:
        return DeployResult(
            tag=tag,
            instance_name=instance_name(tag),
            region=region,
            bundle_id=bundle_id,
            blueprint_id=KIRO_CREW_BLUEPRINT_ID,
            state="DRY_RUN",
            dry_run=True,
            argv=argv,
        )

    existing = find_instance(tag, profile, region)
    reused = existing is not None
    if not reused:
        aws.checked(argv, profile, region, action="lightsail:CreateInstances", timeout=_POLL_TIMEOUT)

    # Wait for the box to reach `running` BEFORE touching the firewall. Lightsail
    # rejects put-instance-public-ports while the instance is `pending` with
    # OperationFailureException ("cannot change the ports of an instance while it
    # is in transition"), so the firewall change must come after the control-plane
    # transition settles. (The gateway coming up is gated separately by the
    # engine's health poll.)
    wait_for_running(tag, profile, region)

    # Open SSH (port 22) only, and only on a new instance: the gateway is
    # loopback-only and reached by tunnel.
    if not reused:
        open_ssh(tag, profile, region, ssh_cidr=ssh_cidr)

    st = describe(tag, profile, region)
    return DeployResult(
        tag=tag,
        instance_name=instance_name(tag),
        region=region,
        instance_id=st.get("instance_id", instance_name(tag)),
        public_ip=st.get("public_ip", ""),
        state=st.get("state", ""),
        bundle_id=bundle_id,
        blueprint_id=KIRO_CREW_BLUEPRINT_ID,
        reused=reused,
        argv=argv,
    )


def build_ssh_port_infos(ssh_cidr: str = "") -> list[str]:
    """The ``--port-infos`` argument for the SSH rule.

    With no CIDR the rule is the plain shorthand (Lightsail's default sources,
    any IPv4 and IPv6 address). With a CIDR it is JSON naming that one IPv4 range
    and an empty ``ipv6Cidrs``, so the IPv6 firewall does not stay open to ``::/0``.
    """
    if not ssh_cidr:
        return [f"fromPort={SSH_PORT},toPort={SSH_PORT},protocol=tcp"]
    return [
        json.dumps(
            [
                {
                    "fromPort": SSH_PORT,
                    "toPort": SSH_PORT,
                    "protocol": "tcp",
                    "cidrs": [ssh_cidr],
                    "ipv6Cidrs": [],
                }
            ],
            separators=(",", ":"),
        )
    ]


def open_ssh(tag: str, profile: str = "", region: str = "", ssh_cidr: str = "") -> None:
    """Open SSH (port 22) on the instance firewall, and nothing else.

    Lightsail's default firewall already allows SSH, but a baked blueprint or a
    narrowed default could differ, so this makes the one port the SSH tunnel
    needs explicit — and, critically, does NOT open the dashboard port, mirroring
    the EC2 security group's "no inbound dashboard port" posture.
    """
    tag = validate_tag(tag)
    profile = validate_profile(profile)
    region = validate_region(region)
    ssh_cidr = validate_ssh_cidr(ssh_cidr)
    aws.checked(
        [
            "lightsail",
            "put-instance-public-ports",
            "--instance-name",
            instance_name(tag),
            "--port-infos",
            *build_ssh_port_infos(ssh_cidr),
        ],
        profile,
        region,
        action="lightsail:PutInstancePublicPorts",
        timeout=_POLL_TIMEOUT,
    )


def wait_for_running(tag: str, profile: str = "", region: str = "") -> str:
    """Poll until the instance reaches ``running`` (or the budget expires).

    Returns the final observed state. This is the fast control-plane transition
    only; the gateway-health wait is the engine's.
    """
    deadline = time.monotonic() + _RUNNING_TIMEOUT
    state = ""
    while time.monotonic() < deadline:
        st = describe(tag, profile, region)
        state = st.get("state", "")
        if state == "running":
            return state
        time.sleep(_RUNNING_POLL_SECS)
    # Timed out before `running`. Opening the firewall now would fail with the
    # same in-transition OperationFailureException, so fail loud and specific
    # instead of letting the caller hit a confusing downstream error.
    raise aws.AWSError(
        f"Lightsail instance {instance_name(tag)} did not reach 'running' within "
        f"{_RUNNING_TIMEOUT}s (last observed state: {state or 'unknown'}).",
        action="lightsail:GetInstanceState",
    )


# --- discovery / status -----------------------------------------------------


def find_instance(tag: str, profile: str = "", region: str = "") -> Optional[dict[str, Any]]:
    """Return the Lightsail instance for ``tag``, or None if absent.

    Only a genuine "does not exist" is reported as absent (``None``); any other
    non-zero exit (throttling, expired SSO, AccessDenied) is raised as an
    :class:`aws.AWSError`, so callers never mistake a transient failure for
    "already gone".

    A same-named instance that is NOT tagged ``kirocrew:managed=true`` is treated
    as a collision and raises, exactly like the EC2 lane's ``find_stack`` guard,
    so ``deploy`` never reuses — and ``destroy`` never deletes — an instance Kiro
    Crew did not create.
    """
    rc, out, err = aws.run_aws(
        ["lightsail", "get-instance", "--instance-name", instance_name(tag), "--output", "json"],
        profile,
        region,
        timeout=_POLL_TIMEOUT,
    )
    if rc != 0:
        # Lightsail returns NotFoundException / "Does not exist" when truly absent.
        if "NotFound" in err or "does not exist" in err or "Does not exist" in err:
            return None
        missing = aws.map_missing_action(err)
        raise aws.AWSError(
            f"could not query Lightsail instance {instance_name(tag)}: {err.strip()[:300]}",
            action="lightsail:GetInstance",
            missing_action=missing,
            returncode=rc,
            stderr=err,
        )
    import json

    try:
        inst = json.loads(out or "{}").get("instance") or {}
    except json.JSONDecodeError:
        return None
    if not inst:
        return None
    tags = {t.get("key"): t.get("value") for t in inst.get("tags", [])}
    if tags.get(MANAGED_TAG_KEY) != MANAGED_TAG_VALUE:
        raise aws.AWSError(
            f"a Lightsail instance named {instance_name(tag)} already exists but is NOT tagged "
            f"{MANAGED_TAG_KEY}={MANAGED_TAG_VALUE} — refusing to touch it (it wasn't created by "
            "Kiro Crew). Use a different --tag, or remove that instance.",
            action="lightsail:GetInstance",
        )
    inst_tag = tags.get(INSTANCE_TAG_KEY)
    if inst_tag is not None and inst_tag != tag:
        raise aws.AWSError(
            f"instance {instance_name(tag)} is {MANAGED_TAG_KEY}={MANAGED_TAG_VALUE} but its "
            f"{INSTANCE_TAG_KEY} tag is '{inst_tag}', not '{tag}' — refusing to touch it. "
            "Use the correct --tag.",
            action="lightsail:GetInstance",
        )
    return inst


def describe(tag: str, profile: str = "", region: str = "") -> dict[str, Any]:
    """Full status for one instance: existence + state + public IP + plan."""
    tag = validate_tag(tag)
    profile = validate_profile(profile)
    region = validate_region(region)
    inst = find_instance(tag, profile, region)
    if not inst:
        return {"tag": tag, "exists": False}
    state = (inst.get("state") or {}).get("name", "")
    return {
        "tag": tag,
        "exists": True,
        "instance_name": inst.get("name", instance_name(tag)),
        # The instance name is the stable id we address it by (register/stop/etc).
        "instance_id": inst.get("name", instance_name(tag)),
        "state": state,
        "public_ip": inst.get("publicIpAddress", ""),
        "bundle_id": inst.get("bundleId", ""),
        "blueprint_id": inst.get("blueprintId", ""),
        "region": (inst.get("location") or {}).get("regionName", region),
        "username": inst.get("username", "ubuntu"),
    }


def key_path(region: str) -> "Path":
    """Local path of the stored Lightsail default private key for ``region``.

    One key per region under ``config_dir()/cloud-lightsail-keys/<region>.pem``.
    Lightsail's default key pair is per-region, so every instance in a region
    shares it; storing by region matches that and avoids a key file per instance.
    """
    from pathlib import Path as _Path

    from kiro_crew.config.loader import config_dir

    region = validate_region(region)
    base = config_dir() / "cloud-lightsail-keys"
    base.mkdir(parents=True, exist_ok=True)
    return _Path(base) / f"{region or 'default'}.pem"


def download_key(profile: str = "", region: str = "", *, force: bool = False) -> "Path":
    """Fetch the region's Lightsail default private key and store it 0600 locally.

    Runs ``aws lightsail download-default-key-pair`` (a read of the caller's own
    account) and writes the PEM to :func:`key_path` with owner-only permissions,
    which ``ssh`` requires. Idempotent: a key already present is reused unless
    ``force`` is set. Returns the key path.

    This is the SSH analog of the EC2 lane's "no key needed" (SSM) model: the
    Lightsail lane has no SSM, so the SSH tunnel needs a private key, and the
    region default key is the one Lightsail puts on every instance created
    without an explicit key pair, which is how this lane launches.
    """
    import os as _os

    profile = validate_profile(profile)
    region = validate_region(region)
    path = key_path(region)
    if path.exists() and not force:
        return path
    data = aws.checked_json(
        ["lightsail", "download-default-key-pair"],
        profile,
        region,
        action="lightsail:DownloadDefaultKeyPair",
    )
    pem = (data or {}).get("privateKeyBase64", "") if isinstance(data, dict) else ""
    if not pem:
        raise aws.AWSError(
            "Lightsail returned no default key pair for this region. The default key may have "
            "been deleted; connect over SSH needs it. Recreate the region default key in the "
            "Lightsail console, or attach your own key at launch.",
            action="lightsail:DownloadDefaultKeyPair",
        )
    # Field name is historical ("Base64") but the value is the PEM text itself.
    # Created with 0600 from the start (never world-readable, even briefly), then
    # moved into place atomically.
    tmp = path.with_suffix(".pem.tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    fd = _os.open(tmp, _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL, 0o600)
    with _os.fdopen(fd, "w") as fh:
        fh.write(pem)
    tmp.replace(path)
    return path


def list_instances(profile: str = "", region: str = "") -> list[dict[str, Any]]:
    """List all launcher-managed Lightsail instances live by tag (stateless-by-tag)."""
    profile = validate_profile(profile)
    region = validate_region(region)
    data = aws.checked_json(
        ["lightsail", "get-instances"],
        profile,
        region,
        action="lightsail:GetInstances",
    )
    out: list[dict[str, Any]] = []
    for inst in data.get("instances", []) if isinstance(data, dict) else []:
        tags = {t.get("key"): t.get("value") for t in inst.get("tags", [])}
        if tags.get(MANAGED_TAG_KEY) != MANAGED_TAG_VALUE:
            continue
        tag = tags.get(INSTANCE_TAG_KEY, "")
        if not tag:
            continue
        out.append(
            {
                "tag": tag,
                "instance_id": inst.get("name", ""),
                "instance_state": (inst.get("state") or {}).get("name", ""),
                "public_ip": inst.get("publicIpAddress", ""),
            }
        )
    return sorted(out, key=lambda r: r["tag"])


# --- pause / resume ---------------------------------------------------------


def stop(tag: str, profile: str = "", region: str = "") -> dict[str, Any]:
    """Stop the instance (pauses compute billing)."""
    aws.assert_human_action("lightsail:StopInstance")
    tag = validate_tag(tag)
    profile = validate_profile(profile)
    region = validate_region(region)
    if find_instance(tag, profile, region) is None:
        raise aws.AWSError(f"no Kiro Crew Lightsail instance found for tag '{tag}'")
    aws.checked(
        ["lightsail", "stop-instance", "--instance-name", instance_name(tag)],
        profile,
        region,
        action="lightsail:StopInstance",
        timeout=_POLL_TIMEOUT,
    )
    return {"tag": tag, "instance_id": instance_name(tag), "action": "stop"}


def start(tag: str, profile: str = "", region: str = "") -> dict[str, Any]:
    """Start a stopped instance."""
    aws.assert_human_action("lightsail:StartInstance")
    tag = validate_tag(tag)
    profile = validate_profile(profile)
    region = validate_region(region)
    if find_instance(tag, profile, region) is None:
        raise aws.AWSError(f"no Kiro Crew Lightsail instance found for tag '{tag}'")
    aws.checked(
        ["lightsail", "start-instance", "--instance-name", instance_name(tag)],
        profile,
        region,
        action="lightsail:StartInstance",
        timeout=_POLL_TIMEOUT,
    )
    return {"tag": tag, "instance_id": instance_name(tag), "action": "start"}


# --- destroy (full uninstall / remove from AWS) -----------------------------


def build_destroy_argv(tag: str) -> list[str]:
    """The exact ``delete-instance`` argv (also the dry-run output)."""
    return ["lightsail", "delete-instance", "--instance-name", instance_name(tag)]


def destroy(
    tag: str,
    profile: str = "",
    region: str = "",
    *,
    wait: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Full teardown: ``delete-instance`` removes the Lightsail instance.

    Idempotent: deleting an already-gone instance is a no-op success. When
    ``wait`` is true this blocks on :func:`wait_for_delete` and returns its
    verdict as ``destroyed``.
    """
    if not dry_run:
        aws.assert_human_action("lightsail:DeleteInstance")
    tag = validate_tag(tag)
    profile = validate_profile(profile)
    region = validate_region(region)

    argv = build_destroy_argv(tag)
    if dry_run:
        return {
            "tag": tag,
            "instance_name": instance_name(tag),
            "action": "destroy",
            "dry_run": True,
            "argv": argv,
        }

    inst = find_instance(tag, profile, region)
    if not inst:
        return {
            "tag": tag,
            "instance_name": instance_name(tag),
            "action": "destroy",
            "destroyed": True,
            "already_absent": True,
        }

    aws.checked(argv, profile, region, action="lightsail:DeleteInstance", timeout=_POLL_TIMEOUT)
    if not wait:
        return {
            "tag": tag,
            "instance_name": instance_name(tag),
            "action": "destroy",
            "destroyed": True,
            "waited": False,
        }

    final = wait_for_delete(tag, profile, region)
    return {
        "tag": tag,
        "instance_name": instance_name(tag),
        "action": "destroy",
        "destroyed": final,
        "waited": True,
    }


def wait_for_delete(tag: str, profile: str = "", region: str = "") -> bool:
    """Block until the instance is gone. Returns True once ``get-instance`` reports absent."""
    deadline = time.monotonic() + _RUNNING_TIMEOUT
    while time.monotonic() < deadline:
        if find_instance(tag, profile, region) is None:
            return True
        time.sleep(_RUNNING_POLL_SECS)
    return find_instance(tag, profile, region) is None
