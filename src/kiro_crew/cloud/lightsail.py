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

**Bootstrap, mirroring EC2.** The shipping EC2 lane does NOT use a pre-baked AMI:
it boots a stock Amazon Linux 2023 image and installs Kiro Crew from UserData,
gating a CloudFormation WaitCondition on the gateway coming healthy. Lightsail has
no WaitCondition, so this lane does the equivalent with a ``--user-data``
cloud-init script that runs the public one-line installer, and the launch engine
(:mod:`kiro_crew.cloud.lightsail_engine`) polls the instance to healthy itself.
``blueprint_id`` defaults to a stock Ubuntu LTS blueprint and the bootstrap path;
when a published Kiro Crew Lightsail blueprint exists it can be passed instead to
skip the install wait — one code path, a config knob, so Phase 2 is useful today
and upgrades to the baked image the day the blueprint publishes.

Discovery is **stateless-by-tag**, like the EC2 lane: every instance carries the
``kirocrew:managed=true`` + ``kirocrew:instance=<tag>`` tags (Lightsail tags), and
the instance name is deterministic (``kirocrew-<tag>``), so
``status``/``stop``/``start``/``destroy`` find the right instance without a local
cache that could drift.
"""

from __future__ import annotations

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

# deploy waits can take many minutes (instance boot + cloud-init install). The
# per-call aws timeout is small (these are quick control-plane calls); the long
# wait for health is the launch engine's poll loop, not a single CLI call.
_POLL_TIMEOUT = 60
# How long to wait for `create-instances` to settle to a `running` instance
# BEFORE the engine begins its gateway health poll. The box reaching `running`
# is a quick Lightsail control-plane transition; the slow part (cloud-init
# install) is gated separately by the engine.
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
# Lightsail blueprint ids look like ``ubuntu_22_04`` / ``amazon_linux_2`` or a
# published custom blueprint id; charset-validate since it reaches argv.
_BLUEPRINT_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,128}\Z")
_BLUEPRINT_SPEC = FieldSpec(name="blueprint_id", type=str, max_len=128, pattern=_BLUEPRINT_RE)
# Lightsail bundle ids look like ``large_3_0`` / ``xlarge_3_0``.
_BUNDLE_RE = re.compile(r"^[a-zA-Z0-9_]{1,64}\Z")
_BUNDLE_SPEC = FieldSpec(name="bundle_id", type=str, max_len=64, pattern=_BUNDLE_RE)

#: The default base blueprint when none is passed: a current Ubuntu LTS image,
#: onto which the cloud-init bootstrap installs Kiro Crew. This is the Option-B
#: path (stock OS + bootstrap) that mirrors how the EC2 lane works today. When a
#: published Kiro Crew Lightsail blueprint exists, pass its id to ``deploy`` to
#: skip the install (Option A) — the lane is otherwise unchanged.
DEFAULT_BLUEPRINT_ID = "ubuntu_22_04"

#: The public one-line installer the bootstrap runs. Kept in step with the
#: install guide (``docs/guides/install.md``). An operator can override the whole
#: user-data via ``deploy(user_data=...)``.
_INSTALL_URL = "https://download.crew.kiro.dev/cli.sh"

#: Official Kiro CLI installer. kiro-cli is a HARD runtime prerequisite for the
#: agent backend (see kiro_prerequisite.py) and the KiroCrew installer above does
#: NOT pull it, so a stock-OS cloud box has the gateway but no agent runtime. We
#: install it in the bootstrap for parity with the EC2 lane, which installs
#: kiro-cli as part of its source install.sh flow. Sign-in stays a post-connect
#: human step (device flow needs a browser; see login.py).
_KIRO_CLI_INSTALL_URL = "https://cli.kiro.dev/install"


def validate_profile(profile: str) -> str:
    return validate_field(profile, _PROFILE_SPEC) or ""


def validate_region(region: str) -> str:
    return validate_field(region, _REGION_SPEC) or ""


def validate_tag(tag: str) -> str:
    val = validate_field(tag, _TAG_SPEC)
    if not val:
        raise ValidationError("tag", "required")
    return val


def validate_blueprint_id(blueprint_id: str) -> str:
    return validate_field(blueprint_id, _BLUEPRINT_SPEC) or ""


def validate_bundle_id(bundle_id: str) -> str:
    return validate_field(bundle_id, _BUNDLE_SPEC) or ""


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


def default_cloud_init(
    *, install_url: str = _INSTALL_URL, kiro_cli_url: str = _KIRO_CLI_INSTALL_URL
) -> str:
    """The cloud-init ``--user-data`` script that installs Kiro Crew on first boot.

    Mirrors the EC2 lane's UserData intent: install the CLI via the public
    one-line installer as the instance's default non-root user, install the
    gateway as a lingering systemd service, and leave the dashboard bound to
    loopback (the SSH tunnel is how it is reached). Lightsail's Ubuntu blueprint
    runs user-data as root on first boot, so the install runs under the default
    ``ubuntu`` user via ``sudo -u``.

    It is intentionally small and idempotent-ish: a re-run of user-data does not
    happen on Lightsail (user-data runs once at creation), and the engine's health
    poll is what gates "ready", so this script does not itself signal readiness.
    """
    # The gateway binds 127.0.0.1 by default; we do NOT open any dashboard port
    # in the Lightsail firewall (see deploy()). Reached via `kirocrew cloud
    # connect` over an SSH tunnel.
    # POSIX sh, NOT bash: Lightsail's Ubuntu blueprint wraps user-data with its
    # own init preamble (SSH CA setup) before running it, which pushes our
    # shebang off line 1 so the script executes under /bin/sh (dash). dash has
    # no `set -o pipefail` ("Illegal option -o pipefail"), so a `#!/bin/bash` +
    # `set -euxo pipefail` dies on line 2 before anything installs. We also do
    # NOT want pipefail regardless: install.sh's progress spinner writes \\r
    # through a pipe, and pipefail turns a harmless SIGPIPE into a fatal
    # mid-install kill (the EC2 lane documents the same). Keep it `set -ex`.
    return f"""#!/bin/sh
set -ex
# Run the install as the default non-root user, not root.
RUN_USER="${{SUDO_USER:-ubuntu}}"
id "$RUN_USER" >/dev/null 2>&1 || RUN_USER=ubuntu
export DEBIAN_FRONTEND=noninteractive
apt-get update -y || true
# curl + ca-certificates for the installers; unzip is a REQUIRED dependency of
# the kiro-cli installer ("Missing required dependencies: unzip") and is not on
# a stock Ubuntu Lightsail image.
apt-get install -y curl ca-certificates unzip || true
# One-line installer, as the non-root user (writes to its home).
sudo -u "$RUN_USER" -H bash -lc 'curl -fsSL {install_url} | sh'
# Install kiro-cli, the agent-backend runtime KiroCrew requires to run turns.
# The KiroCrew installer above does not pull it, so without this the box has a
# gateway but no agent runtime. Non-fatal (|| true): a kiro-cli install hiccup
# should not abort the whole bootstrap and leave no gateway; sign-in and a
# retry can happen post-connect.
sudo -u "$RUN_USER" -H bash -lc 'curl -fsSL {kiro_cli_url} | bash' || true
# Install + enable the gateway as a lingering user service so it survives logout.
sudo -u "$RUN_USER" -H bash -lc 'kirocrew service install' || true
loginctl enable-linger "$RUN_USER" || true
"""


def build_deploy_argv(
    *,
    tag: str,
    availability_zone: str,
    blueprint_id: str,
    bundle_id: str,
    user_data: str = "",
) -> list[str]:
    """Assemble the exact ``aws lightsail create-instances`` argv (also the dry-run output).

    Tags the instance ``kirocrew:managed=true`` + ``kirocrew:instance=<tag>`` so
    discovery is stateless-by-tag, mirroring the EC2 lane. ``user_data`` is passed
    only when non-empty, so an Option-A launch against a pre-baked blueprint (no
    install needed) emits no ``--user-data``.
    """
    argv = [
        "lightsail",
        "create-instances",
        "--instance-names",
        instance_name(tag),
        "--availability-zone",
        availability_zone,
        "--blueprint-id",
        blueprint_id,
        "--bundle-id",
        bundle_id,
        "--tags",
        f"key={MANAGED_TAG_KEY},value={MANAGED_TAG_VALUE}",
        f"key={INSTANCE_TAG_KEY},value={tag}",
    ]
    if user_data:
        argv += ["--user-data", user_data]
    return argv


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
    blueprint_id: str = "",
    availability_zone: str = "",
    user_data: Optional[str] = None,
    dry_run: bool = False,
) -> DeployResult:
    """Provision a Lightsail instance for the given tier. Idempotent by instance name.

    ``tier`` carries the abstract size (``light``/``balanced``/``power``) for
    display/consistency with the EC2 lane; ``bundle_id`` is the concrete Lightsail
    bundle the size resolves to (see :mod:`kiro_crew.cloud.sizes` ``lightsail_bundle``).

    ``blueprint_id`` defaults to :data:`DEFAULT_BLUEPRINT_ID` (stock Ubuntu +
    cloud-init bootstrap). Pass a published Kiro Crew blueprint id to launch the
    pre-baked image instead (no install step). ``user_data`` overrides the
    bootstrap script; it is only sent on the bootstrap path. On a pre-baked
    blueprint the default is NO user-data (the image already runs the gateway),
    so pass ``user_data=""`` is implicit — the bootstrap default is used ONLY on
    the default blueprint.

    ``dry_run`` returns the exact argv without calling AWS.
    """
    if not dry_run:
        aws.assert_human_action("lightsail:CreateInstances")
    tag = validate_tag(tag)
    profile = validate_profile(profile)
    region = validate_region(region)
    bundle_id = validate_bundle_id(bundle_id)
    if not bundle_id:
        raise ValidationError("bundle_id", "required")
    blueprint_id = validate_blueprint_id(blueprint_id) or DEFAULT_BLUEPRINT_ID
    az = availability_zone or default_availability_zone(region)

    # The bootstrap script is only needed on the stock-OS path. On a pre-baked
    # Kiro Crew blueprint the image already runs the gateway, so no user-data.
    if user_data is None:
        user_data = default_cloud_init() if blueprint_id == DEFAULT_BLUEPRINT_ID else ""

    argv = build_deploy_argv(
        tag=tag,
        availability_zone=az,
        blueprint_id=blueprint_id,
        bundle_id=bundle_id,
        user_data=user_data,
    )

    if dry_run:
        return DeployResult(
            tag=tag,
            instance_name=instance_name(tag),
            region=region,
            bundle_id=bundle_id,
            blueprint_id=blueprint_id,
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
    # transition settles. (The slow cloud-init install is gated separately by the
    # engine's gateway health poll.)
    wait_for_running(tag, profile, region)

    # Open SSH (port 22) only — the gateway is loopback-only and reached by tunnel.
    open_ssh(tag, profile, region)

    st = describe(tag, profile, region)
    return DeployResult(
        tag=tag,
        instance_name=instance_name(tag),
        region=region,
        instance_id=st.get("instance_id", instance_name(tag)),
        public_ip=st.get("public_ip", ""),
        state=st.get("state", ""),
        bundle_id=bundle_id,
        blueprint_id=blueprint_id,
        reused=reused,
        argv=argv,
    )


def open_ssh(tag: str, profile: str = "", region: str = "") -> None:
    """Open SSH (port 22) on the instance firewall, and nothing else.

    Lightsail's default firewall already allows SSH, but a baked blueprint or a
    narrowed default could differ, so this makes the one port the SSH tunnel
    needs explicit — and, critically, does NOT open the dashboard port, mirroring
    the EC2 security group's "no inbound dashboard port" posture.
    """
    tag = validate_tag(tag)
    profile = validate_profile(profile)
    region = validate_region(region)
    aws.checked(
        [
            "lightsail",
            "put-instance-public-ports",
            "--instance-name",
            instance_name(tag),
            "--port-infos",
            f"fromPort={SSH_PORT},toPort={SSH_PORT},protocol=tcp",
        ],
        profile,
        region,
        action="lightsail:PutInstancePublicPorts",
        timeout=_POLL_TIMEOUT,
    )


def wait_for_running(tag: str, profile: str = "", region: str = "") -> str:
    """Poll until the instance reaches ``running`` (or the budget expires).

    Returns the final observed state. This is the fast control-plane transition
    only; the gateway-health wait (the slow cloud-init install) is the engine's.
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
    region default key is the one Lightsail puts on every instance it creates
    from a stock blueprint.
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
    tmp = path.with_suffix(".pem.tmp")
    tmp.write_text(pem)
    _os.chmod(tmp, 0o600)
    tmp.replace(path)
    _os.chmod(path, 0o600)
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
