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
  has no WaitCondition, so this engine polls the gateway to healthy ITSELF after
  the instance reaches ``running``. The step therefore still means "create the
  instance and install Kiro Crew", exactly as the EC2 step label promises.

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

#: How long ``provision`` waits for the gateway to come healthy after the box
#: reaches ``running``, and how often it re-probes, in seconds. The box installs
#: Kiro Crew from cloud-init on first boot (apt + the one-line installer + the
#: service), which from a cold image is a few minutes; the EC2 lane gives its
#: WaitCondition 1500s, so this matches that order. The budget is a CEILING: the
#: poll returns on the first healthy probe.
_HEALTH_TIMEOUT_SECONDS = 1500
_HEALTH_POLL_SECONDS = 15

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

    ``blueprint_id`` defaults to the stock-Ubuntu bootstrap path (the same
    strategy the EC2 lane uses: boot a stock OS, install at launch). Passing a
    published Kiro Crew Lightsail blueprint id switches to the pre-baked image
    (no install wait) with no other change — one code path, a config knob.
    """

    def __init__(self, *, blueprint_id: str = "") -> None:
        self._blueprint_id = blueprint_id

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

    def provision(self, *, tag: str, size_key: str, profile: str, region: str) -> str:
        """Create the Lightsail instance and wait for the gateway to come healthy.

        Returns the instance id (the Lightsail instance name) ``register`` and the
        lifecycle verbs address it by. The size key resolves to a Lightsail bundle
        via :func:`kiro_crew.cloud.sizes.lightsail_bundle`; the EC2 ``SizeTier`` is
        still looked up so the lane shares the same tier identity, but it is the
        bundle that is launched.
        """
        tier = sizes.get_tier(size_key)
        bundle = sizes.lightsail_bundle(size_key)
        result = lightsail.deploy(
            tag=tag,
            tier=tier,
            bundle_id=bundle.bundle_id,
            profile=profile,
            region=region,
            blueprint_id=self._blueprint_id,
        )
        # The box is `running`, but on the bootstrap path the gateway is still
        # installing from cloud-init. Gate on health here, as the EC2 lane's
        # WaitCondition does, so the step does not report done before the crew
        # can actually serve. On a pre-baked blueprint this returns on the first
        # probe.
        self._wait_for_gateway(result.instance_id or lightsail.instance_name(tag), profile, region)
        return result.instance_id or lightsail.instance_name(tag)

    def _wait_for_gateway(self, instance_id: str, profile: str, region: str) -> None:
        """Poll the box until its gateway reports healthy, bounded by the budget.

        Health is read with ``lightsail.describe`` reaching ``running`` plus the
        box having a public IP (the minimum for the SSH tunnel to work); the
        gateway's own readiness is confirmed by the user's first ``cloud connect``.
        A deeper in-box probe would need an SSH round trip this engine does not own
        a transport for, so the honest ceiling here is "the box is up and
        reachable"; the connect step's detail tells the user to finish sign-in,
        which also exercises the gateway. Never raises on timeout: the box exists
        and is billing, so the launch proceeds to register (making it visible)
        rather than failing and stranding it.
        """
        deadline = _monotonic() + _HEALTH_TIMEOUT_SECONDS
        while _monotonic() < deadline:
            try:
                st = lightsail.describe(_tag_of(instance_id), profile, region)
            except AWSError:
                # A transient read failure must not fail provisioning: the box is
                # up either way. Retry until the budget runs out.
                st = {}
            if st.get("state") == "running" and st.get("public_ip"):
                return
            _sleep(_HEALTH_POLL_SECONDS)
        logger.info(
            "Lightsail instance %s did not report a running IP within %ss; continuing to "
            "register it so it is visible",
            instance_id,
            _HEALTH_TIMEOUT_SECONDS,
        )

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
            name=f"Kiro Crew Cloud ({tag})",
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
