# Configuration

The whole configuration is one YAML document. Give it either in the environment variable
`ISCSI_CONFIG` or as a file whose path is in `ISCSI_CONFIG_FILE`; exactly one of them.

Any error in the YAML stops the container and nothing is exported: unknown or missing keys, invalid
names, duplicate LUN numbers or names, the same file or device for two LUNs. Things that only show at
start (a missing LUN file, a missing interface, a setting the kernel rejects) make only that LUN,
portal or target fail; the rest runs and the container reports unhealthy.

## Example

```yaml
metrics:
  port: 9482
targets:
  - iqn: iqn.2026-01.example.host:storage
    portal:
      addresses:
        - 192.0.2.10
        - fd00::10
      port: 3260
    max_connections: 8
    chap: true
    parameters:
      MaxBurstLength: 1048576
      InitialR2T: false
    attributes:
      default_cmdsn_depth: 128
    luns:
      - lun: 0
        name: data
        file: /luns/data.img
      - lun: 1
        name: disk2
        device: /dev/disk/by-id/example
    acl:
      - initiator: iqn.1991-05.com.microsoft:client
```

## Reference

### Top level

| Key | Meaning |
|---|---|
| `targets` | List of targets, required. |
| `metrics.port` | Port of `/metrics`, default 9482. |
| `metrics.address` | Address `/metrics` binds to; default all addresses. |
| `module.wait` | Longest wait for the module service, default `15m`. Seconds, or a number with `s`, `m` or `h`. Only used when a target asks for MC/S. |
| `module.trusted_srcversions` | Only if you patched your kernel yourself: list of `srcversion` values of `iscsi_target_mod` (see `/sys/module/iscsi_target_mod/srcversion`) with which MC/S is allowed. No module service is needed then. |

### Target

| Key | Meaning |
|---|---|
| `iqn` | Name of the target, required. |
| `portal` | Where the target listens, required: either `addresses` (list of IPv4 or IPv6 addresses) or `interfaces` (list of interface names: all addresses of each, read at start), and optionally `port` (default 3260). |
| `max_connections` | Connections per session. Default 1; more switches MC/S on. It only takes effect with a patched `iscsi_target_mod`, otherwise it is ignored with a warning and the container reports unhealthy. |
| `chap` | `true`: CHAP is always required. `false`: CHAP is off, passwords that are set are ignored. Not set: CHAP is required as soon as one password is set for the target. |
| `parameters` | Further iSCSI parameters of the target, passed to the kernel as they are. `true` and `false` are written as `Yes` and `No`. |
| `attributes` | Further attributes of the target, passed to the kernel as they are. `true` and `false` are written as `1` and `0`. |
| `luns` | List of LUNs, required. |
| `acl` | List of entries with `initiator`: the initiator names that may log in, required. Every initiator sees all LUNs of the target. |

`MaxConnections`, `authentication` and `generate_node_acls` are managed by the container and cannot
be set under `parameters` or `attributes`.

### LUN

| Key | Meaning |
|---|---|
| `lun` | LUN number, required. Numbers never shift. |
| `name` | Name, required, unique in the whole YAML. The serial number the initiator sees is derived from it and stays the same across restarts. |
| `file` | Path of a file in the container. Exactly one of `file` and `device`. |
| `device` | Path of a block device in the container. |
| `write_back` | Only for files. Default `false`: writes are synchronous, nothing is lost on a power failure. `true` is faster and uses the host's page cache. |
| `attributes` | Further attributes of the backstore, passed to the kernel as they are. |

## Binding

With `network_mode: host`, interfaces are those of the host under their host names. Binding by
interface includes every address of it, also a globally routed IPv6 address; without a firewall the
target is then reachable from the internet. Prefer `addresses` with the stable ones.

## MC/S and the module service

Without the fix in the kernel, stopping a target while a session with several connections is logged
in can crash the host. The module service builds the patched module for the running Debian kernel
and the target loads it. The target allows MC/S only if the loaded module is that one (or one you
named in `module.trusted_srcversions`). In every other case it runs with single connections, logs a
prominent warning and, if the YAML asks for MC/S, reports unhealthy.

A module that is loaded once stays loaded until the next reboot. After the first build on a host on
which the unpatched module is already loaded, reboot once.

On the Windows initiator use the MCS policy "Least Queue Depth" and add connections up to the
configured number.
