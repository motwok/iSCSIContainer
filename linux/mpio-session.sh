#!/bin/sh
# Opens, shows and closes several iSCSI sessions to one target on a Linux initiator (open-iscsi)
# and bundles them with dm-multipath, so one LUN can use several network links.
# open-iscsi has no MC/S (one connection per session); several sessions plus multipath are the
# Linux way. Needs root, iscsiadm (open-iscsi), multipath and multipathd (multipath-tools), udevadm.
set -eu

usage() {
    cat <<'EOF'
Usage:
  mpio-session.sh -t <target-iqn> -p <portal> [-P <port>] [-n <sessions>] [-s <policy>] [-r]
                  [-c <chap-user>] [-m <mutual-user>]
  mpio-session.sh -t <target-iqn> -S
  mpio-session.sh -t <target-iqn> -D [-f]
  mpio-session.sh -p <portal> [-P <port>] -L

  -t <iqn>      name of the target
  -p <portal>   IPv4 or IPv6 address of the target portal
  -P <port>     TCP port of the portal, default 3260
  -n <count>    number of sessions, default 16; each session is one TCP connection
  -s <policy>   multipath path selector: queue-length (default), round-robin or service-time
  -r            log out every session first, then log in again (new spread over the links)
  -c <user>     log in with CHAP as <user> (the user of "iscsictl chap set" on the target)
  -m <user>     mutual CHAP: the target authenticates as <user> ("iscsictl chap set-mutual")
  -S            show the sessions and the multipath devices of the target
  -D            log out every session to the target and remove its multipath devices
  -f            with -D: also when a device is still in use (mounted, held by LVM, ...)
  -L            list the targets of the portal and the sessions to each
  -h            this help

Without -c and -m the CHAP settings of the target's node record stay as they are. The secrets are
asked for without echo; without a terminal they are read from standard input, one per line (first
the CHAP secret, then the mutual secret). They are kept in the node record of open-iscsi (only
readable by root) and are passed to iscsiadm on its command line.

Before more than one session is opened, every LUN of the target must be a multipath device;
several sessions to a LUN without multipath look like independent disks and destroy its data.
EOF
}

die() {
    echo "ERROR: $*" >&2
    exit 1
}

TARGET=
PORTAL=
PORT=3260
SESSIONS=16
POLICY=queue-length
RECONNECT=0
CHAP_USER=
MUTUAL_USER=
ACTION="set"
FORCE=0
while getopts t:p:P:n:s:rc:m:SDLfh opt; do
    case "$opt" in
        t) TARGET=$OPTARG ;;
        p) PORTAL=$OPTARG ;;
        P) PORT=$OPTARG ;;
        n) SESSIONS=$OPTARG ;;
        s) POLICY=$OPTARG ;;
        r) RECONNECT=1 ;;
        c) CHAP_USER=$OPTARG ;;
        m) MUTUAL_USER=$OPTARG ;;
        S) ACTION=show ;;
        D) ACTION=disconnect ;;
        L) ACTION=list ;;
        f) FORCE=1 ;;
        h) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
shift $((OPTIND - 1))
[ "$#" -eq 0 ] || { usage >&2; exit 2; }

case "$ACTION" in
    list) [ -n "$PORTAL" ] || die "-L needs -p" ;;
    set) if [ -z "$TARGET" ] || [ -z "$PORTAL" ]; then die "-t and -p are needed"; fi ;;
    *) [ -n "$TARGET" ] || die "-t is needed" ;;
esac
case "$SESSIONS" in
    ''|*[!0-9]*|0) die "-n must be a number of at least 1" ;;
esac
case "$PORT" in
    ''|*[!0-9]*) die "-P must be a port number" ;;
esac
case "$POLICY" in
    queue-length|round-robin|service-time) SELECTOR="$POLICY 0" ;;
    *) die "-s must be queue-length, round-robin or service-time" ;;
esac
[ -z "$MUTUAL_USER" ] || [ -n "$CHAP_USER" ] || die "-m needs -c"

[ "$(id -u)" -eq 0 ] || die "run this script as root"
for tool in iscsiadm udevadm; do
    command -v "$tool" > /dev/null || die "$tool is missing (packages open-iscsi and udev)"
done

case "$PORTAL" in
    *:*) PORTAL_SPEC="[$PORTAL]:$PORT" ;;
    *) PORTAL_SPEC="$PORTAL:$PORT" ;;
esac

# "tcp: [3] 192.0.2.10:3260,1 iqn... (non-flash)": session ids of the target, ascending.
session_ids() {
    iscsiadm -m session 2> /dev/null | awk -v t="$1" '$4 == t { gsub(/[][]/, "", $2); print $2 }' |
        sort -n
}

count_sessions() {
    session_ids "$1" | wc -l | tr -d ' '
}

# SCSI disks of one session, e.g. "sdb".
session_disks() {
    iscsiadm -m session -r "$1" -P 3 2> /dev/null | awk '/Attached scsi disk/ { print $4 }'
}

# The id multipath uses for a disk (udev ID_SERIAL).
disk_wwid() {
    udevadm info --query=property --name="/dev/$1" | sed -n 's/^ID_SERIAL=//p'
}

# WWIDs of the LUNs the target exports to us, one per line.
target_wwids() {
    for sid in $(session_ids "$TARGET"); do
        for disk in $(session_disks "$sid"); do
            disk_wwid "$disk"
        done
    done | sort -u
}

multipath_ready() {
    command -v multipath > /dev/null && command -v multipathd > /dev/null || return 1
    multipathd show daemon > /dev/null 2>&1
}

map_name() {
    multipathd show maps format "%w %n" 2> /dev/null | awk -v w="$1" 'NR > 1 && $1 == w { print $2 }'
}

map_paths() {
    multipathd show paths format "%w %d %t" 2> /dev/null |
        awk -v w="$1" 'NR > 1 && $1 == w { n++; if ($3 == "active") a++ } END { printf "%d %d\n", a, n }'
}

map_policy() {
    multipath -ll "$1" 2> /dev/null | sed -n "s/.*policy='\([^']*\)'.*/\1/p" | head -n 1
}

config_dir() {
    dir=$(multipath -t 2> /dev/null | awk '$1 == "config_dir" { gsub(/"/, "", $2); print $2; exit }')
    echo "${dir:-/etc/multipath/conf.d}"
}

wait_settle() {
    udevadm settle --timeout=30 || true
}

# The login returns before the kernel has scanned the LUNs; wait until they are there and their
# number stays the same for two seconds.
wait_luns() {
    i=0
    last=-1
    stable=0
    while [ "$i" -lt 30 ]; do
        wait_settle
        n=$(target_wwids | wc -l | tr -d ' ')
        if [ "$n" -gt 0 ] && [ "$n" -eq "$last" ]; then
            stable=$((stable + 1))
            [ "$stable" -lt 2 ] || return 0
        else
            stable=0
        fi
        last=$n
        i=$((i + 1))
        sleep 1
    done
}

read_secret() {
    if [ -t 0 ]; then
        printf '%s: ' "$1" > /dev/tty
        stty -echo < /dev/tty
        IFS= read -r secret < /dev/tty || secret=
        stty echo < /dev/tty
        printf '\n' > /dev/tty
    else
        IFS= read -r secret || secret=
    fi
    [ -n "$secret" ] || die "no secret given for $1"
    [ "${#secret}" -ge 12 ] || die "the secret for $1 has fewer than 12 characters"
    printf '%s' "$secret"
}

node_update() {
    iscsiadm -m node -T "$TARGET" -p "$PORTAL_SPEC" --op update -n "$1" -v "$2" > /dev/null
}

set_chap() {
    [ -n "$CHAP_USER" ] || return 0
    secret=$(read_secret "CHAP secret of $CHAP_USER")
    node_update node.session.auth.authmethod CHAP
    node_update node.session.auth.username "$CHAP_USER"
    node_update node.session.auth.password "$secret"
    if [ -n "$MUTUAL_USER" ]; then
        mutual=$(read_secret "mutual secret of $MUTUAL_USER")
        [ "$mutual" != "$secret" ] || die "the mutual secret must differ from the CHAP secret"
        node_update node.session.auth.username_in "$MUTUAL_USER"
        node_update node.session.auth.password_in "$mutual"
    else
        node_update node.session.auth.username_in ""
        node_update node.session.auth.password_in ""
    fi
}

# Puts every LUN of the target under multipath with the chosen path selector and waits for the
# maps. Without it a second session must not be opened.
setup_multipath() {
    wwids=$(target_wwids)
    [ -n "$wwids" ] || die "the target exports no LUN to this initiator"
    dir=$(config_dir)
    mkdir -p "$dir"
    for wwid in $wwids; do
        cat > "$dir/iscsicontainer-$wwid.conf" <<EOF
# Written by mpio-session.sh for an iSCSI LUN with several sessions.
multipaths {
	multipath {
		wwid "$wwid"
		path_grouping_policy multibus
		path_selector "$SELECTOR"
	}
}
EOF
        multipath -a "$wwid" > /dev/null 2>&1 || true
    done
    multipathd reconfigure > /dev/null
    for wwid in $wwids; do
        i=0
        while [ -z "$(map_name "$wwid")" ]; do
            multipath "$wwid" > /dev/null 2>&1 || true
            i=$((i + 1))
            [ "$i" -le 30 ] || die "no multipath device for LUN $wwid; check that multipathd does not blacklist it"
            sleep 1
        done
    done
}

show_state() {
    ids=$(session_ids "$TARGET")
    if [ -z "$ids" ]; then
        echo "No session to $TARGET."
        return 0
    fi
    echo "$TARGET: $(echo "$ids" | wc -l | tr -d ' ') session(s)"
    if ! multipath_ready; then
        echo "  multipathd is not running"
        return 0
    fi
    for wwid in $(target_wwids); do
        name=$(map_name "$wwid")
        if [ -z "$name" ]; then
            echo "  LUN $wwid: NO multipath device"
            continue
        fi
        paths=$(map_paths "$wwid")
        echo "  /dev/mapper/$name: ${paths% *} of ${paths#* } path(s) active, policy $(map_policy "$wwid")"
    done
}

map_exists() {
    multipathd show maps format "%n" 2> /dev/null | awk 'NR > 1' | grep -qx "$1"
}

# Open count of a device-mapper device (mounted, held by LVM, opened by a program).
map_open_count() {
    dmsetup info -c --noheadings -o open "$1" 2> /dev/null | tr -d ' '
}

disconnect() {
    ids=$(session_ids "$TARGET")
    if [ -z "$ids" ]; then
        echo "No session to $TARGET."
        return 0
    fi
    maps=
    if multipath_ready; then
        for wwid in $(target_wwids); do
            name=$(map_name "$wwid")
            [ -n "$name" ] || continue
            maps="$maps $name"
            open=$(map_open_count "$name")
            if [ "${open:-0}" -gt 0 ] && [ "$FORCE" -eq 0 ]; then
                die "/dev/mapper/$name is in use ($open open); unmount it first or give -f"
            fi
        done
    fi
    for sid in $ids; do
        echo "Logging out session $sid"
        iscsiadm -m session -r "$sid" -u > /dev/null
    done
    # multipathd removes a map once its last path is gone; flush only what is left after that.
    for name in $maps; do
        i=0
        while map_exists "$name" && [ "$i" -lt 15 ]; do
            i=$((i + 1))
            sleep 1
        done
        if map_exists "$name" && ! multipath -f "$name" > /dev/null 2>&1; then
            echo "WARNING: /dev/mapper/$name could not be removed" >&2
        fi
    done
}

list_targets() {
    out=$(iscsiadm -m discovery -t sendtargets -p "$PORTAL_SPEC") || die "discovery at $PORTAL_SPEC failed"
    targets=$(echo "$out" | awk '{ print $2 }' | sort -u)
    [ -n "$targets" ] || { echo "No targets at $PORTAL_SPEC."; return 0; }
    for t in $targets; do
        n=$(count_sessions "$t")
        if [ "$n" -eq 0 ]; then
            echo "$t: not connected"
        else
            echo "$t: connected, $n session(s)"
        fi
    done
    echo "Each session is one TCP connection (open-iscsi has no MC/S); the number of sessions is set with -n."
}

case "$ACTION" in
    show) show_state; exit 0 ;;
    list) list_targets; exit 0 ;;
    disconnect) disconnect; exit 0 ;;
esac

if [ "$SESSIONS" -gt 1 ] && ! multipath_ready; then
    die "more than one session needs dm-multipath: install multipath-tools and start multipathd"
fi

[ "$RECONNECT" -eq 0 ] || disconnect

if [ "$(count_sessions "$TARGET")" -eq 0 ]; then
    echo "Logging in to $TARGET at $PORTAL_SPEC"
    iscsiadm -m discovery -t sendtargets -p "$PORTAL_SPEC" > /dev/null ||
        die "discovery at $PORTAL_SPEC failed"
    iscsiadm -m node -T "$TARGET" -p "$PORTAL_SPEC" > /dev/null 2>&1 ||
        die "the portal does not offer $TARGET"
    set_chap
    if ! iscsiadm -m node -T "$TARGET" -p "$PORTAL_SPEC" --login > /dev/null; then
        if [ -n "$CHAP_USER" ]; then
            die "the login failed; are CHAP user and secret the ones of iscsictl chap set?"
        fi
        die "the login failed; if the target requires CHAP, give -c"
    fi
elif [ -n "$CHAP_USER" ]; then
    set_chap
fi
wait_luns

if [ "$SESSIONS" -gt 1 ] || [ "$(count_sessions "$TARGET")" -gt 1 ]; then
    multipath_ready || die "several sessions are open, but multipathd is not running"
    setup_multipath
fi

first=$(session_ids "$TARGET" | head -n 1)
while [ "$(count_sessions "$TARGET")" -lt "$SESSIONS" ]; do
    iscsiadm -m session -r "$first" --op new > /dev/null ||
        die "session $(( $(count_sessions "$TARGET") + 1 )) could not be opened"
done
while [ "$(count_sessions "$TARGET")" -gt "$SESSIONS" ]; do
    last=$(session_ids "$TARGET" | tail -n 1)
    iscsiadm -m session -r "$last" -u > /dev/null
done
wait_settle

# Wait until multipath sees every new path.
if multipath_ready && [ -n "$(target_wwids)" ]; then
    for wwid in $(target_wwids); do
        [ -n "$(map_name "$wwid")" ] || continue
        i=0
        while :; do
            paths=$(map_paths "$wwid")
            [ "${paths#* }" -ge "$SESSIONS" ] && break
            i=$((i + 1))
            [ "$i" -le 30 ] || break
            sleep 1
        done
    done
fi
show_state
