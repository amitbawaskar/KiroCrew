# Lightsail as a `kirocrew cloud` provider

## Goal

Make AWS Lightsail a first-class provider in `kirocrew cloud`, a peer to the
existing EC2 lane: `launch` / `connect` / `stop` / `start` / `destroy` against a
Lightsail instance in the customer's own account, appearing in the dashboard's
Instances switcher.

## Where commands run

Every `kirocrew cloud` command runs on the machine where Kiro Crew is installed
(a laptop or workstation), using that machine's AWS credentials. Nothing is run
on the instance by the customer except an optional SSH session.

## What the customer gets

```
kirocrew cloud launch --provider lightsail
```

- Pick a fixed-price Lightsail bundle (same `light`/`balanced`/`power` size keys
  as EC2) instead of reasoning about instance types, EBS and data-transfer
  charges. The launch prints the monthly price read from Lightsail for the chosen
  region. SSD and data transfer are included; snapshots and static IPs are billed
  separately.
- The launch calls Lightsail directly with the customer's credentials: no
  CloudFormation stack, no IAM role of its own, no SSM. It does not create or
  attach an IAM role to the instance.
- The instance is created from the published Kiro Crew Lightsail blueprint
  (`kiro_crew_ls_1_0`). Kiro Crew and kiro-cli are already installed and the
  gateway runs as a service. The launch returns once the service is active and
  answering on the instance's loopback address, so the first `connect` works.
- The firewall allows port 22 only (never the dashboard port). `--ssh-cidr CIDR`
  limits it to one IPv4 range (same validation as the EC2 lane) and closes the
  IPv6 rule. Without it SSH is open to any address, and key authentication is the
  only gate. The firewall is set only when the instance is created; relaunching
  an existing tag does not touch it.
- `kirocrew cloud connect --provider lightsail` opens the dashboard over an SSH
  tunnel to the instance's loopback gateway. The crew shows up in the Instances
  switcher.
- **Signing in to Kiro is a manual step.** The launch does not sign the crew in;
  the customer signs in from the dashboard after the first connect.
- `kirocrew cloud stop|start|destroy --provider lightsail` for the full
  lifecycle. A stopped instance gets a new public IP unless a static IP is
  attached, so `start` waits for the instance to run and updates the crew's
  saved address.

## Limitations

- **`--provider lightsail` is needed on every command.** The launch record keeps
  profile, region and tag, not the provider, so a command without the flag takes
  the EC2 path.
- **SSH uses the region's default Lightsail key pair.** Launch creates the
  instance with it, and `connect` downloads it (`download-default-key-pair`) to
  the crew's config directory with mode 0600. If the default key pair was deleted
  after launch, `connect` fails with a message saying so. Short-lived access via
  `get-instance-access-details` is not used.
- **Region support.** The blueprint is checked at launch with `get-blueprints`; a
  region without it is refused before anything is created. Bundle ids and prices
  differ by region (`xlarge_3_0` in us-west-2, `xlarge_3_2` in ap-southeast-2,
  `xlarge_3_1` in ap-south-1 and sa-east-1), so the bundle with the tier's vCPU
  and RAM is resolved with `get-bundles` for the chosen region.

| Tier                      | Lightsail bundle (us-west-2) |
|---------------------------|------------------------------|
| light, 16 GB / 4 vCPU     | `xlarge_3_0`, $84/mo         |
| balanced, 32 GB / 8 vCPU  | `2xlarge_3_0`, $164/mo       |
| power, 64 GB / 16 vCPU    | `4xlarge_3_0`, $384/mo       |

## How it works

| Piece              | EC2                                         | Lightsail                                          |
|--------------------|---------------------------------------------|----------------------------------------------------|
| Provision          | `cloudformation deploy` (stock AL2023 AMI)  | `lightsail create-instances` (Kiro Crew blueprint) |
| Install            | UserData: install + systemd + WaitCondition | Preinstalled in the blueprint; no user-data        |
| Ready check        | WaitCondition                               | SSH probe: service active, loopback port answers   |
| Firewall           | security group, no inbound dashboard port   | `put-instance-public-ports`, port 22 only          |
| Connect            | SSM tunnel + token                          | SSH tunnel (default key pair) + token              |
| stop/start/destroy | CloudFormation stack ops                    | `stop-instance` / `start-instance` / `delete-instance` |

## Change set (all additive; the EC2 path is untouched)

- `src/kiro_crew/cloud/lightsail.py`: the Lightsail analog of `ec2.py`.
  `deploy`/`status`/`stop`/`start`/`destroy`, stateless-by-tag discovery, the
  SSH-only firewall, per-region bundle resolution and the blueprint check.
- `src/kiro_crew/cloud/lightsail_engine.py`: the five-method `LaunchEngine`:
  `provision` (resolve bundle, create, probe the gateway), `register` (SSH
  transport), `begin_signin` (no-prompt handle), `teardown`, plus
  `refresh_registration` for the new IP after a start.
- `src/kiro_crew/cloud/sizes.py`: a Lightsail bundle catalog keyed to the same
  `light`/`balanced`/`power` tier keys, used as a fallback when live bundle data
  cannot be read.
- `src/kiro_crew/cloud/connect.py`: the SSH tunnel and token path,
  `ssh_gateway_active`, `register_instance_ssh` and `update_instance_ssh_host`.
- `src/kiro_crew/platform/defaults.py`: register the `aws_lightsail` provisioner
  (always offered, like EC2) and its `engine_for` branch.
- `src/kiro_crew/cli.py` + `cli_cloud.py`: `--provider {ec2,lightsail}`,
  `--ssh-cidr`, and provider-aware launch/list/status/stop/start/destroy dispatch.
- `src/kiro_crew/cloud/aws.py`: Lightsail read-only ops (`get-instance`,
  `get-instances`, `get-bundles`, `get-blueprints`) added to the agent chokepoint
  allowlist (mutations stay refused).
- `src/kiro_crew/security/denied_rules.py`: `aws-destructive-lightsail-*` rules.
- `docs/system-specs/modules/cloud.md`: the Lightsail lane section.
- `test/test_cloud_lightsail.py`: the lane's tests (fake engine, no live AWS).

## Scope notes

- **Standard SSH-key handling.** The connect path uses the instance's default
  Lightsail key pair; it does not depend on any ephemeral-credential broker.
- **Tests run against a fake engine**, as the EC2 and Fargate lanes do. An
  end-to-end launch, connect, stop, start and destroy in a real account is a
  manual verification step.
