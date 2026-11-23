"""The Lightsail launch engine — binds the launch job's abstract steps to the
``cloud.lightsail`` module.

Implements the five-method :class:`~kiro_crew.cloud.launch_job.LaunchEngine`
Protocol for Lightsail, the parallel of
:class:`~kiro_crew.cloud.launch_engine.RealLaunchEngine` (EC2) and
:class:`~kiro_crew.cloud.fargate_engine.FargateLaunchEngine` (Fargate). Every
AWS call goes through ``cloud.lightsail``, which routes through the single
``aws`` CLI chokepoint in :mod:`kiro_crew.cloud.aws`; there is no ``boto3``.

**Where it differs from the EC2 engine, and why.**

* ``provision`` resolves the size key to a Lightsail BUNDLE (fixed-price plan)
  rather than an EC2 instance type, and creates the instance with
  ``lightsail.deploy``. The EC2 lane blocks inside ``ec2.deploy`` on a
  CloudFormation WaitCondition that gates on the gateway being healthy; Lightsail
  has no WaitCondition, so this engine probes the gateway over SSH ITSELF after
  the instance reaches ``running``, until the ``kirocrew`` service is active and
  answering on loopback.

* ``register`` uses the SSH transport. Lightsail instances have no SSM agent, so
  the box is registered with ``connect.register_instance_ssh`` (``ssh_host =
  user@public-ip``, ``connection_method="ssh"``), which the dashboard's existing
  SSH tunnel reaches — the same transport a hand-added Remote-crew-over-SSH record
  already uses.

* ``begin_signin`` does not drive a device-code flow. The EC2 lane signs the box
  in over SSM; this lane has no SSM, and wiring a second remote-login transport is
  out of scope for the provider. So — exactly like the EC2 engine's own
  SSM-failure fallback and the Fargate handle — it returns a handle that completes
  without a prompt, and the crew is signed in from the dashboard after connect
  (the connect step's detail says so). This keeps the launch reaching ``register``
  so the crew is visible and billing is never invisible.
"""

from __future__ import annotations

import logging
import threading
import time

from kiro_crew.cloud import connect as connect_mod
from kiro_crew.cloud import lightsail, sizes
from kiro_crew.cloud.aws import AWSError
from kiro_crew.cloud.login_target import KiroLoginTarget
from kiro_crew.platform.interfaces import BUILTIN_PROVISIONER_ID

logger = logging.getLogger(__name__)

#: The id a launch request names as ``provider_id`` for the Lightsail lane.
LIGHTSAIL_PROVISIONER_ID = "aws_lightsail"

#: How long ``provision`` waits for the gateway to answer after the box reaches
#: ``running``, and how often it re-probes, in seconds. The blueprint starts the
#: gateway on first boot (Apache, the service), which takes a couple of minutes.
#: The budget is a CEILING: the poll returns on the first healthy probe.
_HEALTH_TIMEOUT_SECONDS = 600
_HEALTH_POLL_SECONDS = 10

# Indirection so tests can patch out the poll sleep.
_sleep = time.sleep
_monotonic = time.monotonic


class LightsailSigninHandle:
    """The sign-in step for Lightsail, which this lane does not drive remotely.

    Mirrors :class:`~kiro_crew.cloud.fargate_engine.FargateSigninHandle`: it
    completes at once, reporting ``already_logged_in`` so ``run_launch`` takes the
    no-prompt branch and the launch proceeds to ``register`` (making the crew
    visible) rather than stranding a provisioned, billing box behind a sign-in
    step that cannot run.

    Unlike Fargate, a Lightsail crew's gateway IS a full dashboard the user will
    sign in to — but over an SSH session the user opens with ``kirocrew cloud
    connect``, not a device-code flow this engine pushes. So this reports no
    prompt and leaves ``error`` empty; the connect step's detail tells the user to
    sign in to Kiro on the crew after they connect.
    """

    def __init__(self, instance_id: str) -> None:
        self.instance_id = instance_id
        self.already_logged_in: bool = True
        self.url: str = ""
        self.code: str = ""
        self.ports: list = []
        self.error: str = ""

    def wait(self, cancel: threading.Event) -> bool:
        """Return immediately; there is no interactive step to wait for.

        The cancel event is honoured: a launch cancelled before this point should
        not report a completed step.
        """
        return not cancel.is_set()

    def close(self) -> None:
        """Nothing to release. No browser, no device-code poller, no session."""

    def abort(self) -> bool:
        """Nothing to stop: this lane starts no remote login, so a cancelled
        sign-in has no poller that could complete later. ``True`` means
        "confirmed there is nothing to stop", explicit so that
        :func:`~kiro_crew.cloud.launch_job._abort_signin` never has to guess what
        a missing method means.
        """
        return True


class LightsailLaunchEngine:
    """Drives a launch against the user's AWS account via :mod:`kiro_crew.cloud.lightsail`.

    Every launch uses the published Kiro Crew Lightsail blueprint. ``ssh_cidr``
    optionally restricts the SSH firewall rule to one IPv4 range.

    ``gateway_ready`` is ``None`` until ``provision`` has run, then ``True`` when
    the gateway answered a probe and ``False`` when the budget ran out (the box
    exists and is billing either way, so the launch still registers it).
    """

    def __init__(self, *, ssh_cidr: str = "") -> None:
        self._ssh_cidr = ssh_cidr
        self.gateway_ready: bool | None = None

    def preflight(self, profile: str, region: str) -> None:
        # Lightsail is reached with the same credentials as EC2; the EC2
        # reachability check (sts + harmless describes) is a sound proxy for "the
        # profile resolves and AWS is reachable", so reuse it rather than add a
        # second probe. A Lightsail-specific permission gap surfaces at the first
        # real ``lightsail`` call with the exact missing action (see cloud.aws).
        from kiro_crew.cloud import iam

        reach = iam.reachability_check(profile, region)
        if not reach.get("reachable"):
            detail = reach.get("detail") or reach.get("note") or "AWS credentials did not resolve"
            raise AWSError(str(detail))
        if not lightsail.blueprint_available(profile, region):
            raise AWSError(
                f"The Kiro Crew Lightsail blueprint ({lightsail.KIRO_CREW_BLUEPRINT_ID}) is not "
                f"available in {region}. Pick a region where it is offered."
            )

    def provision(self, *, tag: str, size_key: str, profile: str, region: str) -> str:
        """Create the Lightsail instance and wait for the gateway to come healthy.

        Returns the instance id (the Lightsail instance name) ``register`` and the
        lifecycle verbs address it by. The size key resolves to the Lightsail bundle with
        the same vCPU and RAM in this region via :func:`lightsail.resolve_bundle`; the
        EC2 ``SizeTier`` is still looked up so the lane shares the same tier
        identity, but it is the bundle that is launched.
        """
        tier = sizes.get_tier(size_key)
        bundle = lightsail.resolve_bundle(size_key, profile, region)
        result = lightsail.deploy(
            tag=tag,
            tier=tier,
            bundle_id=bundle.bundle_id,
            profile=profile,
            region=region,
            ssh_cidr=self._ssh_cidr,
        )
        instance_id = result.instance_id or lightsail.instance_name(tag)
        # The box is `running`, but the gateway starts on first boot. Gate on it
        # here, as the EC2 lane's WaitCondition does, so the step does not report
        # done before the crew can actually serve.
        self.gateway_ready = self._wait_for_gateway(instance_id, profile, region)
        return instance_id

    def _wait_for_gateway(self, instance_id: str, profile: str, region: str) -> bool:
        """Probe the box over SSH until its gateway answers, bounded by the budget.

        Returns True when ``systemctl is-active kirocrew`` succeeds and the
        dashboard port answers on loopback; False when the budget runs out or no
        probe is possible (no key, no public IP). Never raises: the box exists and
        is billing, so the launch proceeds to register (making it visible) rather
        than failing and stranding it.
        """
        tag = _tag_of(instance_id)
        deadline = _monotonic() + _HEALTH_TIMEOUT_SECONDS
        key = ""
        while _monotonic() < deadline:
            try:
                st = lightsail.describe(tag, profile, region)
            except AWSError:
                # A transient read failure must not fail provisioning.
                st = {}
            if st.get("state") == "running" and st.get("public_ip"):
                try:
                    if not key:
                        key = str(lightsail.download_key(profile, region))
                except AWSError as exc:
                    logger.warning("cannot probe %s: no SSH key (%s)", instance_id, exc)
                    return False
                user = st.get("username", "ubuntu") or "ubuntu"
                if connect_mod.ssh_gateway_active(f"{user}@{st['public_ip']}", key):
                    return True
            _sleep(_HEALTH_POLL_SECONDS)
        logger.warning(
            "Lightsail instance %s: the gateway did not answer within %ss; registering it "
            "anyway so it is visible",
            instance_id,
            _HEALTH_TIMEOUT_SECONDS,
        )
        return False

    def begin_signin(
        self,
        *,
        instance_id: str,
        profile: str,
        region: str,
        login_target: KiroLoginTarget | None = None,
    ) -> LightsailSigninHandle:
        """Return a handle that completes at once. See :class:`LightsailSigninHandle`.

        ``login_target`` is accepted because the ``LaunchEngine`` Protocol declares
        it, and is not used: this lane drives no sign-in, so there is no identity
        to pin. A non-default target is NOT refused here (unlike Fargate) because a
        Lightsail crew is a full gateway the user signs in to themselves after
        connect, with whatever identity they choose — the launch does not sign it
        in at all.
        """
        return LightsailSigninHandle(instance_id)

    def register(self, *, instance_id: str, tag: str, profile: str, region: str) -> None:
        """Add the Lightsail box to the Instances registry over the SSH transport.

        Resolves the box's public IP and SSH username from ``lightsail.describe``
        and registers ``ssh_host = <user>@<public-ip>`` with
        ``connection_method="ssh"`` and
        ``provisioner_id=LIGHTSAIL_PROVISIONER_ID``. Fails loudly if the box has no
        public IP (the SSH tunnel needs one) or if the registry write fails — the
        box is running and billing, so a silent miss would leave the user paying
        for an invisible crew, exactly as the EC2 engine's ``register`` guards.
        """
        st = lightsail.describe(tag, profile, region)
        public_ip = st.get("public_ip", "")
        if not public_ip:
            raise RuntimeError(
                f"The crew was created (Lightsail instance {instance_id}) but has no public IP "
                "yet, so it could not be added to your crews. It is running and billing — check "
                "`kirocrew cloud status`, then add it under Remote crew over SSH, or delete it."
            )
        user = st.get("username", "ubuntu") or "ubuntu"
        ssh_host = f"{user}@{public_ip}"
        registered = connect_mod.register_instance_ssh(
            ssh_host,
            name=_crew_name(tag),
            profile=profile,
            region=region,
            provisioner_id=LIGHTSAIL_PROVISIONER_ID,
        )
        if registered is None:
            raise RuntimeError(
                f"The crew was created (Lightsail instance {instance_id}, {ssh_host}) but could "
                "not be added to your crews. It is running and billing — add it under Remote "
                "crew over SSH, or delete it, so it does not sit idle."
            )

    def teardown(self, *, tag: str, profile: str, region: str) -> bool:
        """Delete the Lightsail instance this launch created — the rollback path.

        Returns True only when AWS confirms the instance is gone (``delete-instance``
        then a poll until ``get-instance`` reports absent), mirroring the EC2
        engine's wait-and-confirm so a cancelled or failed launch never reports
        "removed" while the box is still billing. Also removes the box's SSH
        Instances record if one was written.
        """
        st = lightsail.describe(tag, profile, region)
        res = lightsail.destroy(tag, profile, region, wait=False)
        if not res.get("already_absent"):
            public_ip = st.get("public_ip", "") if st.get("exists") else ""
            if public_ip:
                user = st.get("username", "ubuntu") or "ubuntu"
                connect_mod.unregister_instance(f"{user}@{public_ip}")
        return bool(lightsail.wait_for_delete(tag, profile, region))


def _crew_name(tag: str) -> str:
    """The Instances-registry name of the crew launched under ``tag``."""
    return f"Kiro Crew Cloud ({tag})"


def refresh_registration(tag: str, profile: str, region: str) -> bool:
    """Re-point the crew's registry record at the instance's current public IP.

    Called after ``start``: a stopped Lightsail instance comes back with a new
    public IP (unless a static IP is attached), and the record's ``ssh_host`` is
    ``user@public-ip``. Returns True when a record was updated.
    """
    st = lightsail.describe(tag, profile, region)
    ip = st.get("public_ip", "")
    if not ip:
        return False
    user = st.get("username", "ubuntu") or "ubuntu"
    return connect_mod.update_instance_ssh_host(
        name=_crew_name(tag), provisioner_id=LIGHTSAIL_PROVISIONER_ID, ssh_host=f"{user}@{ip}"
    )


def _tag_of(instance_id: str) -> str:
    """The discovery tag embedded in a Lightsail instance name (``kirocrew-<tag>``).

    ``provision`` returns the instance name as the id, and the health poll reads
    status by tag; this strips the deterministic prefix. An id that does not carry
    it (a hand-supplied value) is returned unchanged, so the subsequent describe
    simply reports "not found" rather than this guessing.
    """
    if instance_id.startswith(lightsail.INSTANCE_PREFIX):
        return instance_id[len(lightsail.INSTANCE_PREFIX) :]
    return instance_id


# ``BUILTIN_PROVISIONER_ID`` is imported only to keep this module's provider-id
# constants beside the one it mirrors; referenced here so linters do not flag the
# import as unused when the engine is wired through ``platform.defaults``.
_ = BUILTIN_PROVISIONER_ID
