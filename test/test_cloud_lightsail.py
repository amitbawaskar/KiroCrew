"""Unit tests for the Lightsail cloud lane.

Mirrors the EC2 lane's tests (``test_launch_engine_ref.py`` / ``test_cloud_sizes.py``):
every AWS call is monkeypatched at the ``cloud.aws`` chokepoint, so nothing here
touches a live account. Covers the provider module (``cloud/lightsail.py``), the
Lightsail bundle catalog (``cloud/sizes.py``), and the launch engine
(``cloud/lightsail_engine.py``).
"""

from __future__ import annotations

import threading

import pytest

from kiro_crew.cloud import aws, lightsail, lightsail_engine, sizes


# --- sizes: the Lightsail bundle catalog -------------------------------------
class TestLightsailBundleCatalog:
    def test_same_keys_as_the_interactive_ec2_tiers(self):
        # The lane reuses the EC2 tier keys so the picker is identical.
        assert sizes.LIGHTSAIL_INTERACTIVE_KEYS == sizes.INTERACTIVE_TIER_KEYS

    def test_bundles_match_ec2_tier_vcpu_one_to_one(self):
        # 16 GB=4 vCPU, 32 GB=8 vCPU, 64 GB=16 vCPU — the whole point of the 1:1
        # claim in the pitch (no RAM-floor gap).
        for key in sizes.LIGHTSAIL_INTERACTIVE_KEYS:
            tier = sizes.get_tier(key)
            bundle = sizes.lightsail_bundle(key)
            assert bundle.vcpu == tier.vcpu
            assert bundle.ram_gb == tier.ram_gb

    def test_prices_are_real_fixed_numbers(self):
        for key in sizes.LIGHTSAIL_INTERACTIVE_KEYS:
            b = sizes.lightsail_bundle(key)
            assert b.usd_per_month > 0
            assert b.disk_gb > 0
            assert b.transfer_tb > 0

    def test_unknown_or_x86_key_lists_valid(self):
        # The x86 lane has no Lightsail bundle; refuse with the valid set named.
        with pytest.raises(KeyError) as ei:
            sizes.lightsail_bundle("light-x86")
        assert "light-x86" in str(ei.value)
        assert "balanced" in str(ei.value)

    def test_order_is_light_balanced_power(self):
        keys = [b.key for b in sizes.lightsail_bundles()]
        assert keys == ["light", "balanced", "power"]

    def test_summary_mentions_price(self):
        assert "/mo" in sizes.lightsail_bundle("balanced").summary()


# --- lightsail module: validation + argv -------------------------------------
class TestInstanceNaming:
    def test_instance_name_is_deterministic(self):
        assert lightsail.instance_name("t1") == "kirocrew-t1"

    def test_default_az_is_region_a(self):
        assert lightsail.default_availability_zone("us-west-2") == "us-west-2a"


class TestBuildDeployArgv:
    def test_create_instances_shape_and_tags(self):
        argv = lightsail.build_deploy_argv(
            tag="t1",
            availability_zone="us-west-2a",
            blueprint_id="ubuntu_22_04",
            bundle_id="large_3_0",
            user_data="#!/bin/bash\necho hi\n",
        )
        assert argv[:2] == ["lightsail", "create-instances"]
        assert "kirocrew-t1" in argv
        assert "ubuntu_22_04" in argv
        assert "large_3_0" in argv
        # Both discovery tags present.
        assert f"key={lightsail.MANAGED_TAG_KEY},value=true" in argv
        assert f"key={lightsail.INSTANCE_TAG_KEY},value=t1" in argv
        assert "--user-data" in argv

    def test_no_user_data_when_empty(self):
        argv = lightsail.build_deploy_argv(
            tag="t1",
            availability_zone="us-west-2a",
            blueprint_id="my-baked-blueprint",
            bundle_id="large_3_0",
            user_data="",
        )
        assert "--user-data" not in argv

    def test_destroy_argv(self):
        assert lightsail.build_destroy_argv("t1") == [
            "lightsail",
            "delete-instance",
            "--instance-name",
            "kirocrew-t1",
        ]


class TestBootstrapScript:
    def test_default_cloud_init_installs_as_non_root_and_lingers(self):
        script = lightsail.default_cloud_init()
        assert lightsail._INSTALL_URL in script
        assert "kirocrew service install" in script
        assert "enable-linger" in script
        # Install runs as the non-root user, not root.
        assert "sudo -u" in script


class TestDeployDryRun:
    def test_dry_run_builds_argv_without_calling_aws(self, monkeypatch):
        monkeypatch.setattr(
            aws, "run_aws", lambda *a, **k: pytest.fail("dry run must not hit AWS")
        )
        res = lightsail.deploy(
            tag="t1",
            tier=sizes.default_tier(),
            bundle_id="large_3_0",
            region="us-west-2",
            dry_run=True,
        )
        assert res.dry_run is True
        assert res.argv[:2] == ["lightsail", "create-instances"]
        assert res.blueprint_id == lightsail.DEFAULT_BLUEPRINT_ID

    def test_dry_run_default_blueprint_includes_bootstrap(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: pytest.fail("no AWS"))
        res = lightsail.deploy(
            tag="t1", tier=sizes.default_tier(), bundle_id="large_3_0",
            region="us-west-2", dry_run=True,
        )
        # Default (stock Ubuntu) blueprint → bootstrap user-data present.
        assert "--user-data" in res.argv

    def test_dry_run_baked_blueprint_has_no_bootstrap(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: pytest.fail("no AWS"))
        res = lightsail.deploy(
            tag="t1", tier=sizes.default_tier(), bundle_id="large_3_0",
            region="us-west-2", blueprint_id="kirocrew-baked", dry_run=True,
        )
        # A pre-baked blueprint already runs the gateway → no install user-data.
        assert "--user-data" not in res.argv


class TestFindInstanceOwnershipGuard:
    def _patch_get_instance(self, monkeypatch, payload, rc=0, err=""):
        import json

        def fake_run(argv, profile="", region="", **k):
            return (rc, json.dumps(payload) if rc == 0 else "", err)

        monkeypatch.setattr(aws, "run_aws", fake_run)

    def test_absent_returns_none(self, monkeypatch):
        self._patch_get_instance(monkeypatch, {}, rc=1, err="NotFoundException: does not exist")
        assert lightsail.find_instance("t1", region="us-west-2") is None

    def test_transient_error_raises(self, monkeypatch):
        self._patch_get_instance(monkeypatch, {}, rc=1, err="ThrottlingException")
        with pytest.raises(aws.AWSError):
            lightsail.find_instance("t1", region="us-west-2")

    def test_untagged_collision_raises(self, monkeypatch):
        self._patch_get_instance(
            monkeypatch, {"instance": {"name": "kirocrew-t1", "tags": []}}
        )
        with pytest.raises(aws.AWSError) as ei:
            lightsail.find_instance("t1", region="us-west-2")
        assert "wasn't created by Kiro Crew" in str(ei.value)

    def test_wrong_instance_tag_raises(self, monkeypatch):
        self._patch_get_instance(
            monkeypatch,
            {
                "instance": {
                    "name": "kirocrew-t1",
                    "tags": [
                        {"key": lightsail.MANAGED_TAG_KEY, "value": "true"},
                        {"key": lightsail.INSTANCE_TAG_KEY, "value": "other"},
                    ],
                }
            },
        )
        with pytest.raises(aws.AWSError) as ei:
            lightsail.find_instance("t1", region="us-west-2")
        assert "not this launch's" in str(ei.value) or "refusing to touch" in str(ei.value)

    def test_managed_match_returns_instance(self, monkeypatch):
        self._patch_get_instance(
            monkeypatch,
            {
                "instance": {
                    "name": "kirocrew-t1",
                    "tags": [
                        {"key": lightsail.MANAGED_TAG_KEY, "value": "true"},
                        {"key": lightsail.INSTANCE_TAG_KEY, "value": "t1"},
                    ],
                    "state": {"name": "running"},
                    "publicIpAddress": "203.0.113.5",
                }
            },
        )
        inst = lightsail.find_instance("t1", region="us-west-2")
        assert inst is not None
        assert inst["publicIpAddress"] == "203.0.113.5"


class TestAgentChokepointAllowsReadsOnly:
    """A Lightsail read is allowed from an agent session; a mutation is not."""

    def test_read_ops_allowlisted(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "x")
        for op in ("get-instance", "get-instances", "get-bundles", "get-regions"):
            aws.assert_chokepoint_allowed(["lightsail", op])  # no raise

    def test_mutations_refused_in_agent_session(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "x")
        for op in ("create-instances", "delete-instance", "stop-instance", "put-instance-public-ports"):
            with pytest.raises(aws.CloudActionDenied):
                aws.assert_chokepoint_allowed(["lightsail", op])


# --- lightsail_engine: the 5-method LaunchEngine -----------------------------
class TestLightsailEngine:
    def test_provisioner_id(self):
        assert lightsail_engine.LIGHTSAIL_PROVISIONER_ID == "aws_lightsail"

    def test_provision_resolves_bundle_and_waits(self, monkeypatch):
        seen = {}

        def fake_deploy(**kw):
            seen.update(kw)
            return lightsail.DeployResult(
                tag=kw["tag"],
                instance_name="kirocrew-t1",
                region=kw["region"],
                instance_id="kirocrew-t1",
                public_ip="203.0.113.5",
                state="running",
            )

        monkeypatch.setattr(lightsail, "deploy", fake_deploy)
        # Health poll returns at once (box running + has IP).
        monkeypatch.setattr(
            lightsail,
            "describe",
            lambda *a, **k: {"state": "running", "public_ip": "203.0.113.5"},
        )
        eng = lightsail_engine.LightsailLaunchEngine()
        iid = eng.provision(tag="t1", size_key="balanced", profile="dev", region="us-west-2")
        assert iid == "kirocrew-t1"
        # The size resolved to the Lightsail bundle, not an EC2 instance type.
        assert seen["bundle_id"] == sizes.lightsail_bundle("balanced").bundle_id

    def test_signin_handle_completes_without_prompt(self):
        eng = lightsail_engine.LightsailLaunchEngine()
        handle = eng.begin_signin(instance_id="kirocrew-t1", profile="dev", region="us-west-2")
        assert handle.already_logged_in is True
        assert handle.url == "" and handle.error == ""
        assert handle.wait(threading.Event()) is True
        assert handle.abort() is True
        handle.close()

    def test_register_uses_ssh_transport(self, monkeypatch):
        monkeypatch.setattr(
            lightsail,
            "describe",
            lambda *a, **k: {"public_ip": "203.0.113.5", "username": "ubuntu"},
        )
        seen = {}

        def fake_register_ssh(ssh_host, **kw):
            seen["ssh_host"] = ssh_host
            seen.update(kw)
            return "crew-id"

        monkeypatch.setattr(
            lightsail_engine.connect_mod, "register_instance_ssh", fake_register_ssh
        )
        eng = lightsail_engine.LightsailLaunchEngine()
        eng.register(instance_id="kirocrew-t1", tag="t1", profile="dev", region="us-west-2")
        assert seen["ssh_host"] == "ubuntu@203.0.113.5"
        assert seen["provisioner_id"] == lightsail_engine.LIGHTSAIL_PROVISIONER_ID

    def test_register_fails_loud_without_public_ip(self, monkeypatch):
        monkeypatch.setattr(lightsail, "describe", lambda *a, **k: {"public_ip": ""})
        eng = lightsail_engine.LightsailLaunchEngine()
        with pytest.raises(RuntimeError) as ei:
            eng.register(instance_id="kirocrew-t1", tag="t1", profile="dev", region="us-west-2")
        assert "no public IP" in str(ei.value)

    def test_register_fails_loud_on_registry_miss(self, monkeypatch):
        monkeypatch.setattr(
            lightsail, "describe", lambda *a, **k: {"public_ip": "203.0.113.5", "username": "ubuntu"}
        )
        monkeypatch.setattr(
            lightsail_engine.connect_mod, "register_instance_ssh", lambda *a, **k: None
        )
        eng = lightsail_engine.LightsailLaunchEngine()
        with pytest.raises(RuntimeError) as ei:
            eng.register(instance_id="kirocrew-t1", tag="t1", profile="dev", region="us-west-2")
        assert "could not be added to your crews" in str(ei.value)

    def test_teardown_confirms_only_on_delete(self, monkeypatch):
        monkeypatch.setattr(
            lightsail, "describe", lambda *a, **k: {"exists": True, "public_ip": "", "username": "ubuntu"}
        )
        monkeypatch.setattr(lightsail, "destroy", lambda *a, **k: {"already_absent": False})
        monkeypatch.setattr(lightsail, "wait_for_delete", lambda *a, **k: True)
        eng = lightsail_engine.LightsailLaunchEngine()
        assert eng.teardown(tag="t1", profile="dev", region="us-west-2") is True

    def test_teardown_unconfirmed_is_false(self, monkeypatch):
        monkeypatch.setattr(lightsail, "describe", lambda *a, **k: {"exists": False})
        monkeypatch.setattr(lightsail, "destroy", lambda *a, **k: {"already_absent": True})
        monkeypatch.setattr(lightsail, "wait_for_delete", lambda *a, **k: False)
        eng = lightsail_engine.LightsailLaunchEngine()
        assert eng.teardown(tag="t1", profile="dev", region="us-west-2") is False


# --- platform wiring: the lane is registered ---------------------------------
class TestProvisionerRegistration:
    def test_lightsail_offered_in_default_provisioners(self):
        from kiro_crew.platform.defaults import (
            LIGHTSAIL_PROVISIONER_ID,
            DefaultRemoteProvisionerProvider,
        )

        ids = [p.id for p in DefaultRemoteProvisionerProvider().provisioners()]
        assert LIGHTSAIL_PROVISIONER_ID in ids

    def test_engine_for_lightsail_returns_the_engine(self):
        from kiro_crew.platform.defaults import (
            LIGHTSAIL_PROVISIONER_ID,
            DefaultRemoteProvisionerProvider,
        )

        eng = DefaultRemoteProvisionerProvider().engine_for(LIGHTSAIL_PROVISIONER_ID)
        assert isinstance(eng, lightsail_engine.LightsailLaunchEngine)


# --- deny rules: the destructive Lightsail verbs are blocked -----------------
class TestLightsailDenyRules:
    def test_lightsail_destructive_rules_present(self):
        from kiro_crew.security.denied_rules import BUILTIN_DENIED_RULES

        ids = {r.id for r in BUILTIN_DENIED_RULES}
        assert "aws-destructive-lightsail-delete-instance" in ids
        assert "aws-destructive-lightsail-create-instances" in ids
        assert "aws-destructive-lightsail-instance-lifecycle" in ids


# --- deploy ordering: firewall must open only after the instance is running --
class TestDeployOrdering:
    """Lightsail rejects put-instance-public-ports while an instance is `pending`
    (OperationFailureException, "cannot change the ports ... in transition"). A
    live launch surfaced this because the unit tests mocked each AWS call in
    isolation and could not see call ordering. This records the real sequence and
    pins create-instances -> (running) -> put-instance-public-ports.
    """

    def test_open_ssh_happens_after_running(self, monkeypatch):
        calls: list[str] = []

        def fake_checked(argv, profile, region, **kw):
            # argv[0]="lightsail", argv[1]=the verb
            calls.append(argv[1])
            return 0, "", ""

        # describe is polled by wait_for_running; return `pending` the first time
        # then `running`, so the test proves open_ssh waits for the transition.
        states = iter(["pending", "running", "running"])

        def fake_describe(tag, profile="", region=""):
            try:
                state = next(states)
            except StopIteration:
                state = "running"
            return {"state": state, "public_ip": "203.0.113.5", "instance_id": "kirocrew-t1"}

        monkeypatch.setattr(aws, "checked", fake_checked)
        monkeypatch.setattr(lightsail, "find_instance", lambda *a, **k: None)
        monkeypatch.setattr(lightsail, "describe", fake_describe)
        monkeypatch.setattr(lightsail, "_RUNNING_POLL_SECS", 0)
        monkeypatch.setattr(aws, "assert_human_action", lambda *a, **k: None)

        lightsail.deploy(
            tag="t1",
            tier=sizes.default_tier(),
            bundle_id="large_3_0",
            profile="dev",
            region="us-west-2",
        )

        assert "create-instances" in calls
        assert "put-instance-public-ports" in calls
        # The firewall change must come AFTER the instance was created, and (via
        # the state iterator) only once it reached `running`.
        assert calls.index("create-instances") < calls.index("put-instance-public-ports")


# --- SSH key handling: connect needs the region default key ------------------
class TestDownloadKey:
    def test_downloads_and_stores_0600(self, monkeypatch, tmp_path):
        import os as _os

        from kiro_crew.config import loader as cfg_loader

        monkeypatch.setattr(cfg_loader, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            aws,
            "checked_json",
            lambda *a, **k: {"privateKeyBase64": "-----BEGIN KEY-----\nabc\n-----END KEY-----\n"},
        )
        path = lightsail.download_key(region="us-west-2")
        assert path.exists()
        assert path.read_text().startswith("-----BEGIN KEY-----")
        # Owner-only perms (ssh refuses a group/world-readable key).
        assert (_os.stat(path).st_mode & 0o077) == 0

    def test_reuses_existing_key(self, monkeypatch, tmp_path):
        from kiro_crew.config import loader as cfg_loader

        monkeypatch.setattr(cfg_loader, "config_dir", lambda: tmp_path)
        calls = {"n": 0}

        def fake_json(*a, **k):
            calls["n"] += 1
            return {"privateKeyBase64": "-----BEGIN KEY-----\nx\n-----END KEY-----\n"}

        monkeypatch.setattr(aws, "checked_json", fake_json)
        p1 = lightsail.download_key(region="us-west-2")
        p2 = lightsail.download_key(region="us-west-2")
        assert p1 == p2
        assert calls["n"] == 1  # second call reused the cached key, no AWS call

    def test_raises_when_no_key_returned(self, monkeypatch, tmp_path):
        from kiro_crew.config import loader as cfg_loader

        monkeypatch.setattr(cfg_loader, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(aws, "checked_json", lambda *a, **k: {})
        with pytest.raises(aws.AWSError):
            lightsail.download_key(region="us-west-2")


# --- SSH connect path (Lightsail lane) ---------------------------------------
class TestConnectSsh:
    def _patch_ports(self, monkeypatch, *, free=True, ready=True):
        from kiro_crew.cloud import ssm

        monkeypatch.setattr(ssm, "port_is_free", lambda *a, **k: free)
        monkeypatch.setattr(ssm, "wait_for_local_port", lambda *a, **k: ready)

    def test_refuses_occupied_local_port(self, monkeypatch):
        from kiro_crew.cloud import connect

        self._patch_ports(monkeypatch, free=False)
        conn = connect.connect_ssh("ubuntu@203.0.113.5", "/tmp/k.pem", open_browser=False)
        assert conn.ready is False
        assert "already in use" in conn.error

    def test_happy_path_mints_token_and_builds_url(self, monkeypatch):
        from kiro_crew.cloud import connect

        self._patch_ports(monkeypatch, ready=True)

        class _Proc:
            def poll(self):
                return None  # still alive -> owns the port

            def wait(self):
                return 0

        monkeypatch.setattr(connect, "open_ssh_forward", lambda *a, **k: _Proc())
        monkeypatch.setattr(connect, "mint_token_ssh", lambda *a, **k: "JWT123")
        monkeypatch.setattr(connect, "_open_browser", lambda url: True)
        conn = connect.connect_ssh(
            "ubuntu@203.0.113.5", "/tmp/k.pem", local_port=5599, open_browser=True
        )
        assert conn.ready is True
        assert conn.token == "JWT123"
        assert "token=JWT123" in conn.url
        assert conn.browser_opened is True

    def test_tears_down_when_token_mint_fails(self, monkeypatch):
        from kiro_crew.cloud import connect

        self._patch_ports(monkeypatch, ready=True)
        terminated = {"n": 0}

        class _Proc:
            def poll(self):
                return None

        monkeypatch.setattr(connect, "open_ssh_forward", lambda *a, **k: _Proc())
        monkeypatch.setattr(connect, "mint_token_ssh", lambda *a, **k: "")
        monkeypatch.setattr(connect, "_terminate", lambda p: terminated.__setitem__("n", 1))
        conn = connect.connect_ssh("ubuntu@203.0.113.5", "/tmp/k.pem", open_browser=False)
        assert conn.ready is False
        assert "token" in conn.error
        assert terminated["n"] == 1  # the tunnel was torn down, no orphan

    def test_refuses_when_ssh_child_exited_but_port_answers(self, monkeypatch):
        # Free-check -> bind race: a listener answering while our ssh child is
        # dead is a foreign process; the token must NOT be routed to it.
        from kiro_crew.cloud import connect

        self._patch_ports(monkeypatch, ready=True)

        class _DeadProc:
            def poll(self):
                return 1  # exited

        monkeypatch.setattr(connect, "open_ssh_forward", lambda *a, **k: _DeadProc())
        minted = {"n": 0}
        monkeypatch.setattr(
            connect, "mint_token_ssh", lambda *a, **k: minted.__setitem__("n", 1) or "JWT"
        )
        monkeypatch.setattr(connect, "_terminate", lambda p: None)
        conn = connect.connect_ssh("ubuntu@203.0.113.5", "/tmp/k.pem", open_browser=False)
        assert conn.ready is False
        assert minted["n"] == 0  # never minted a token for a foreign listener

    def test_ssh_target_defaults_user(self):
        from kiro_crew.cloud import connect

        assert connect._ssh_target("203.0.113.5") == "ubuntu@203.0.113.5"
        assert connect._ssh_target("admin@203.0.113.5") == "admin@203.0.113.5"
