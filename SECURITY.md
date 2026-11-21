# Security Policy

iSCSIContainer runs privileged, loads kernel modules on the host and manages the host kernel's iSCSI
target, which exports block storage over the network. That makes it security-sensitive by nature, and
reports are taken seriously.

## Reporting a vulnerability

**Please report vulnerabilities privately; do not open a public issue, pull request, or discussion for a
security problem.**

Use **GitHub's private vulnerability reporting**:

1. Go to the repository's **Security** tab ->
   **[Report a vulnerability](https://github.com/motwok/iSCSIContainer/security/advisories/new)**.
2. Describe the issue with enough detail to reproduce and assess it.

This keeps the report private between you and the maintainer and integrates with GitHub Security
Advisories for a coordinated fix and disclosure.

### What to include

- A clear description of the vulnerability and its **impact**.
- **Steps to reproduce** (or a proof of concept).
- Affected **version** (image tag or digest), the host's distribution, kernel and Docker version, and
  the initiator involved.
- Any suggested mitigation, if you have one.

## What to expect

This is a single-maintainer project, so responses are best-effort rather than bound to a fixed SLA. You
can expect an acknowledgement of your report, an assessment, and, for confirmed issues, a coordinated fix
and disclosure. Credit is given to reporters who want it.

Please give a reasonable amount of time for a fix before any public disclosure.

## Supported versions

Only the **latest release** (`:latest`) receives security fixes. Older releases are
not patched; please update to the newest one.

| Version | Supported |
|---|---|
| Latest release (`:latest`) | yes |
| Older releases | no |

## Base image updates

The Debian base image is referenced by tag, and every build upgrades its packages. A daily workflow
detects changed package versions and turns them into a patch release, so **security fixes of Debian
reach `:latest` within about a day**. These updates and those proposed by Dependabot are merged automatically once the build passes,
so a vulnerable or compromised update would be shipped as well. If you want to review updates first,
pin a version tag instead of `:latest`. Details:
[RELEASING.md](RELEASING.md#base-image-and-dependency-updates).

## Scope

iSCSIContainer deliberately does things that can look alarming but are by design:

- it runs **privileged**, usually in the **host network**,
- it **loads kernel modules** of the host (`target_core_*`, `iscsi_target_mod`) and mounts **configfs**,
- it **clears the host kernel's target configuration** on start and on stop, and builds its own from
  the YAML on start,
- it accesses the **LUN files or block devices** configured as backstores,
- its module service **downloads the Debian kernel source for the running kernel and builds a
  patched `iscsi_target_mod`** from it, and the target **loads it** into the host kernel (unsigned;
  with Secure Boot on it cannot be loaded); with any other `iscsi_target_mod` the target runs
  without MC/S,
- `/metrics` exposes **target and initiator names (IQNs)** and session data. With the host network its
  port is on the host; do not expose it to untrusted networks,
- it keeps **CHAP secrets in plain text** (one-way and mutual) in files on its config volume and in the
  kernel's configfs; CHAP requires the target to know the plain secret, so a hash cannot be used.
  Protect the volume with file permissions,
- CHAP per target follows `chap:` in the YAML: `true` always requires it, `false` switches it off and
  ignores passwords that are set (logged as a warning); without the setting, **one password set for
  one initiator requires CHAP from every initiator** of that target, and a target without any password
  only checks the initiator name (IQN), which is logged as a warning.

These documented behaviors are **not** vulnerabilities on their own. Access control to the LUNs is the
target configuration's job (ACLs, CHAP); exporting a LUN without them is a configuration choice. Genuine
security issues include, for example: a LUN reachable by an initiator the configuration does not allow;
a login without CHAP to a target that has a CHAP password set and not `chap: false`; CHAP secrets
disclosed in logs, in the output of a command or in files readable by others than the container's
user; changes to the host
beyond the target configuration and the modules listed; or execution of untrusted or
attacker-controlled code.

## Security considerations for operators

These follow from how iSCSI and LIO work. They are not bugs, but you should know them when you deploy.

### Authentication

- **The initiator name (IQN) is not a secret.** Without CHAP, anyone who can reach the portal and knows
  or guesses an allowed IQN gets the LUN. Use CHAP unless the network itself is trusted.
- **CHAP is all or nothing per target.** LIO switches authentication per target, not per initiator.
  With authentication off, an initiator could log in without CHAP even if CHAP credentials exist for
  its IQN. iSCSIContainer therefore turns CHAP on for every initiator of a target as soon as one
  password is set; initiators without a password are refused from then on.
- **Explicit setting per target:** `chap: true` in the YAML always requires CHAP, `chap: false`
  switches it off and ignores passwords that are set. Use `chap: true` where CHAP must never be off by
  accident, e.g. after passwords were removed.
- **No silent downgrade.** If a password is set but cannot be read, the target stays in CHAP mode and
  the affected initiator is refused. Only `chap: false` switches CHAP off, and the container logs a
  warning when it ignores passwords that way.
- **Mutual CHAP** lets the initiator check the target as well; it protects against a fake target that
  collects writes. Use it where an attacker could redirect traffic.
- **CHAP is weak against offline guessing.** Whoever records a login can try passwords offline against
  the MD5 exchange. Use long random secrets. The Windows initiator limits secrets to 12 to 16
  characters, so use the full 16 random characters there, and a mutual secret different from the
  initiator's.
- **Target discovery is not authenticated.** A client that reaches the portal can list the target
  names (SendTargets) even without CHAP; it cannot open a LUN without passing the ACL and CHAP. To show
  initiators only the targets whose ACL lists them, set `demo_mode_discovery: "0"` under `attributes:`
  of each target.

### Secrets

- CHAP secrets are stored in **plain text** in files on the config volume (`/config/chap/`, mode
  `0600`) and in the kernel's configfs; CHAP cannot work with hashes. Anyone with root on the host or
  access to the volume can read them.
- The config volume therefore belongs to the protected part of the host, and **its backups contain the
  secrets** too.
- Passwords are set with a command inside the container. Anyone who may run `docker exec` there has
  root on the host anyway; there is no additional boundary.

### Network

- **iSCSI traffic is not encrypted.** Data on the LUN and the CHAP exchange travel in clear text. Run
  iSCSI only on a trusted network segment or VLAN; iSCSIContainer does not provide IPsec.
- **Bind the portal only where it is needed.** The YAML must name the interfaces or addresses
  for every target; there is no default. With the host network, the host firewall applies to the
  portal port.
- **Binding by interface includes global IPv6 addresses.** If the interface has a globally routed
  IPv6 address, the portal listens on it too, and **without a firewall the target is reachable from
  the internet**. Addresses are read at start only; an address with a changing provider prefix stops
  working after the prefix changes. Prefer `addresses:` with the stable addresses (IPv4 and ULA).

### Data safety

- **Never let two initiators use the same LUN at the same time** unless they run a cluster file system
  or a multipath (MPIO) setup made for it. Two independent sessions writing to one LUN destroy its
  file system. An ACL with several initiators on one target allows exactly that; the container logs a
  warning for it at every start. The same file or device for two LUNs is refused as a configuration
  error.
- **Several paths from one initiator only with MPIO or MC/S.** Two sessions from the same initiator to
  one LUN without MPIO look like two independent disks to the client and destroy the file system. On
  clients without MPIO, such as Windows 10/11, use **MC/S** instead: one session with several
  connections.
- The container clears and rebuilds the whole kernel target configuration on start and clears it on
  stop. **Nothing else on the host may manage LIO** (for example a `targetcli` or restore service on the
  host); its configuration would be wiped or would conflict.
