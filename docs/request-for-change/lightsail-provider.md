# Phase 2 — Lightsail as a `kirocrew cloud` provider

## Goal

Make AWS Lightsail a first-class provider in `kirocrew cloud`, a peer to the
existing EC2 lane: `launch` / `connect` / `stop` / `start` / `destroy` against a
Lightsail instance in the customer's own account, appearing in the dashboard's
Instances switcher. This is the CLI/dashboard provider path (Phase 2 of the
kirocrew-on-lightsail pitch), distinct from the one-click Lightsail *console
blueprint* (Phase 1).

## What the customer gets

```
kirocrew cloud launch --provider lightsail
```

- Pick a FIXED-PRICE Lightsail bundle (same `light`/`balanced`/`power` size keys
  as EC2) instead of reasoning about instance types, EBS and data-transfer
  charges. One predictable monthly number, bandwidth and SSD included.
- No IAM role, no CloudFormation, no SSM. The instance comes up with Kiro Crew
  installed as a service, firewall locked to port 22 only.
- `kirocrew cloud connect --provider lightsail` opens the dashboard over an SSH
  tunnel; the crew shows up in the Instances switcher.
- `kirocrew cloud stop|start|destroy --provider lightsail` for the full
  lifecycle.

The headline benefit is cost: for the same specs Lightsail is cheaper on compute
alone and the price *includes* storage and bandwidth the EC2 quote does not even
count — and the lane can show the real `$X/month` where the EC2 lane only shows an
illustrative hourly rate.

| Tier (same specs)         | EC2 compute alone, 24×7 | Lightsail fixed bundle |
|---------------------------|-------------------------|------------------------|
| light — 16 GB / 4 vCPU    | ~$98/mo                 | **$84/mo**             |
| balanced — 32 GB / 8 vCPU | ~$238/mo                | **$164/mo**            |
| power — 64 GB / 16 vCPU   | ~$477/mo                | **$384/mo**            |

## How it works (mirrors the EC2 lane)

The shipping EC2 lane does NOT use a pre-baked image: it boots a stock OS and
installs Kiro Crew at launch, gating a CloudFormation WaitCondition on the gateway
being healthy. Lightsail Phase 2 does the exact same strategy with Lightsail
primitives.

| Piece          | EC2 (today)                                   | Lightsail (Phase 2)                                    |
|----------------|-----------------------------------------------|--------------------------------------------------------|
| Provision      | `cloudformation deploy` (stock AL2023 AMI)    | `lightsail create-instances` (stock Ubuntu blueprint)  |
| Install        | UserData: install + systemd + WaitCondition   | cloud-init `--user-data`: one-line installer, poll health |
| Firewall       | security group, no inbound dashboard port     | `put-instance-public-ports`, port 22 only              |
| Connect        | SSM tunnel + token                            | SSH tunnel (public SSH key) + token                    |
| stop/start/destroy | CloudFormation stack ops                  | `stop`/`start`/`delete-instance`                       |

`blueprint_id` defaults to the stock-Ubuntu bootstrap path and accepts a published
Kiro Crew Lightsail blueprint id to switch to the pre-baked image (no install
wait) — one code path, a config knob. So Phase 2 is useful immediately and
upgrades to the baked image the day Phase 1's blueprint publishes.

## Change set (all additive; the EC2 path is untouched)

- `src/kiro_crew/cloud/lightsail.py` — the Lightsail analog of `ec2.py`:
  `deploy`/`status`/`stop`/`start`/`destroy`, stateless-by-tag discovery, SSH-only
  firewall, the cloud-init bootstrap.
- `src/kiro_crew/cloud/lightsail_engine.py` — the five-method `LaunchEngine`:
  `provision` (resolve bundle, create, poll health), `register` (SSH transport),
  `begin_signin` (no-prompt handle), `teardown`.
- `src/kiro_crew/cloud/sizes.py` — a Lightsail bundle catalog keyed to the same
  `light`/`balanced`/`power` tier keys, with real fixed monthly prices.
- `src/kiro_crew/cloud/connect.py` — `register_instance_ssh`, the SSH sibling of
  `register_instance`.
- `src/kiro_crew/platform/defaults.py` — register the `aws_lightsail` provisioner
  (always offered, like EC2) and its `engine_for` branch.
- `src/kiro_crew/cli.py` + `cli_cloud.py` — `--provider {ec2,lightsail}` and
  provider-aware launch/list/status/stop/start/destroy dispatch.
- `src/kiro_crew/cloud/aws.py` — Lightsail read-only ops added to the agent
  chokepoint allowlist (mutations stay refused).
- `src/kiro_crew/security/denied_rules.py` — `aws-destructive-lightsail-*` rules.
- `docs/system-specs/modules/cloud.md` — the Lightsail lane section.
- `test/test_cloud_lightsail.py` — the lane's tests (fake engine, no live AWS).

## Scope notes

- **GIAD is out of scope.** The public SSH-connect path uses standard SSH-key
  handling. (GIAD was an Amazon-internal ephemeral-cred broker not in the public
  repo; dropped at the pitch owner's direction.)
- **The published blueprint is a later optimization.** Phase 2 works today on the
  stock-Ubuntu bootstrap path, exactly as the EC2 lane works without a baked AMI;
  the `blueprint_id` knob flips to the pre-baked image when it publishes.
- **A live launch is the author's to run.** The implementation is unit-tested
  against a fake engine (as EC2/Fargate are); an end-to-end `create-instances` in
  a real account is a human verification step.
