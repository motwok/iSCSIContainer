#!/bin/sh
# linux/mpio-session.sh against the real target, with open-iscsi and dm-multipath of the host.
# Needs root, open-iscsi with iscsid running, multipath-tools with multipathd running, Docker.
# TARGET_IMAGE: image to test. The host's initiator name must be INITIATOR below.
set -eu
TARGET_IMAGE=${TARGET_IMAGE:-iscsicontainer:test}
SCRIPT=$(cd "$(dirname "$0")/../.." && pwd)/linux/mpio-session.sh
IQN=iqn.2026-11.test.mpio:target
INITIATOR=iqn.2026-11.test.integration:init
SECRET=mpio-secret-123
MUTUAL=mpio-mutual-456
mpio() { sh "$SCRIPT" -t "$IQN" -p 127.0.0.1 "$@"; }

cleanup() {
    sh "$SCRIPT" -t "$IQN" -D -f > /dev/null 2>&1 || true
    docker rm -f mpio-target > /dev/null 2>&1 || true
    docker volume rm -f mpio-luns mpio-config > /dev/null 2>&1 || true
}
fail() {
    echo "FAILED: $1"
    sh "$SCRIPT" -t "$IQN" -S || true
    multipath -ll || true
    docker logs mpio-target 2>&1 | tail -30 || true
    cleanup
    exit 1
}
config() {
    cat <<YAML
targets:
  - iqn: $IQN
    portal:
      addresses:
        - 127.0.0.1
    luns:
      - lun: 0
        name: mpio0
        file: /luns/mpio0.img
      - lun: 1
        name: mpio1
        file: /luns/mpio1.img
    acl:
      - initiator: $INITIATOR
YAML
}
sessions() {
    iscsiadm -m session 2> /dev/null | awk -v t="$IQN" '$4 == t' | wc -l | tr -d ' '
}
maps() {
    multipathd show maps format "%n %w" | awk 'NR > 1 { print $1 }' | while read -r m; do
        multipath -ll "$m" | grep -q "LIO-ORG" && echo "$m"
    done
}
paths() {
    multipath -ll "$1" | grep -c " sd[a-z]* "
}

cleanup
[ "$(sed -n 's/^InitiatorName=//p' /etc/iscsi/initiatorname.iscsi)" = "$INITIATOR" ] ||
    fail "the initiator name is not $INITIATOR"
docker volume create mpio-luns > /dev/null
docker volume create mpio-config > /dev/null
docker run --rm -v mpio-luns:/luns --entrypoint sh "$TARGET_IMAGE" \
    -c 'truncate -s 64M /luns/mpio0.img /luns/mpio1.img'
docker run -d --name mpio-target --privileged --network host \
    -v /lib/modules:/lib/modules:ro -v /sys/kernel/config:/sys/kernel/config \
    -v mpio-luns:/luns -v mpio-config:/config -e ISCSI_CONFIG="$(config)" "$TARGET_IMAGE" > /dev/null
for i in $(seq 1 60); do
    docker exec mpio-target iscsictl health > /dev/null 2>&1 && break
    [ "$i" -lt 60 ] || fail "target not healthy"
    sleep 1
done

echo "== list"
out=$(sh "$SCRIPT" -p 127.0.0.1 -L)
echo "$out"
echo "$out" | grep -q "^$IQN: not connected" || fail "list"

echo "== 4 sessions"
mpio -n 4
[ "$(sessions)" = 4 ] || fail "not 4 sessions"
set -- $(maps)
[ "$#" = 2 ] || fail "expected 2 multipath devices, found $#"
for m in "$@"; do
    [ "$(paths "$m")" = 4 ] || fail "$m has not 4 paths"
    multipath -ll "$m" | grep -q "policy='queue-length 0'" || fail "$m policy"
done

echo "== write and read through multipath"
head -c 8388608 /dev/urandom > /tmp/mpio.data
dd if=/tmp/mpio.data of="/dev/mapper/$1" bs=1M oflag=direct status=none
dd if="/dev/mapper/$1" of=/tmp/mpio.back bs=1M count=8 iflag=direct status=none
cmp /tmp/mpio.data /tmp/mpio.back || fail "data read back differs"

echo "== show"
mpio -S | tee /tmp/mpio.show
grep -q "4 session(s)" /tmp/mpio.show || fail "show"
grep -c "4 of 4 path(s) active" /tmp/mpio.show | grep -qx 2 || fail "show paths"

echo "== 2 sessions"
mpio -n 2
[ "$(sessions)" = 2 ] || fail "not 2 sessions"
for m in $(maps); do [ "$(paths "$m")" = 2 ] || fail "$m has not 2 paths"; done

echo "== disconnect refuses a device in use"
first=$(maps | head -n 1)
# shellcheck disable=SC2016
sh -c 'exec 3< "$1"; sleep 60' holder "/dev/mapper/$first" &
holder=$!
sleep 1
if sh "$SCRIPT" -t "$IQN" -D; then fail "disconnect did not refuse"; fi
[ "$(sessions)" = 2 ] || fail "sessions gone after the refusal"
kill "$holder"
wait "$holder" 2> /dev/null || true

echo "== disconnect"
sh "$SCRIPT" -t "$IQN" -D
[ "$(sessions)" = 0 ] || fail "sessions left"
[ -z "$(maps)" ] || fail "multipath devices left"

echo "== round-robin"
mpio -n 2 -s round-robin
for m in $(maps); do multipath -ll "$m" | grep -q "policy='round-robin 0'" || fail "$m policy"; done
sh "$SCRIPT" -t "$IQN" -D

echo "== CHAP"
echo "$SECRET" | docker exec -i mpio-target iscsictl chap set "$IQN" "$INITIATOR" mpiouser
if echo "wrong-secret-99" | mpio -n 2 -c mpiouser; then fail "login with a wrong secret"; fi
[ "$(sessions)" = 0 ] || fail "session with a wrong secret"
echo "$SECRET" | mpio -n 2 -c mpiouser
[ "$(sessions)" = 2 ] || fail "CHAP: not 2 sessions"

echo "== mutual CHAP"
echo "$MUTUAL" | docker exec -i mpio-target iscsictl chap set-mutual "$IQN" "$INITIATOR" mpiotarget
printf '%s\n%s\n' "$SECRET" "$MUTUAL" | mpio -r -n 2 -c mpiouser -m mpiotarget
[ "$(sessions)" = 2 ] || fail "mutual CHAP: not 2 sessions"
sh "$SCRIPT" -t "$IQN" -D

echo "== more than one session needs multipathd"
systemctl stop multipathd.socket multipathd > /dev/null 2>&1 || true
if mpio -n 2 2> /tmp/mpio.err; then fail "two sessions without multipathd"; fi
grep -q "needs dm-multipath" /tmp/mpio.err || fail "message without multipathd"
[ "$(sessions)" = 0 ] || fail "session without multipathd"
systemctl start multipathd > /dev/null 2>&1 || true

cleanup
echo "OK"
