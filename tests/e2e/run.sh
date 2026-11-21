#!/bin/sh
# Module service and target together on the running kernel, with the real images.
# Needs root rights on the Docker host: loads kernel modules and configures the kernel target.
# TARGET_IMAGE, MODULE_IMAGE: images to test. HOST_MODULES: where the host's /lib/modules is.
# EXPECT: module status the kernel must give (default: original-unpatched, any non-Debian kernel).
set -eu
TARGET_IMAGE=${TARGET_IMAGE:-iscsicontainer:test}
MODULE_IMAGE=${MODULE_IMAGE:-iscsicontainer-module:test}
HOST_MODULES=${HOST_MODULES:-/lib/modules}
EXPECT=${EXPECT:-original-unpatched}
IQN=iqn.2026-11.test.e2e:target
TPG=/sys/kernel/config/target/iscsi/$IQN/tpgt_1

cleanup() {
    docker rm -f e2e-target e2e-module > /dev/null 2>&1 || true
    docker volume rm -f e2e-modules e2e-luns > /dev/null 2>&1 || true
}
fail() {
    echo "FAILED: $1"
    docker logs e2e-module 2>&1 | tail -20 || true
    docker logs e2e-target 2>&1 | tail -40 || true
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
    max_connections: $1
    luns:
      - lun: 0
        name: e2e
        file: /luns/e2e.img
    acl:
      - initiator: iqn.2026-11.test.e2e:init
YAML
}
target() {
    docker rm -f e2e-target > /dev/null 2>&1 || true
    docker run -d --name e2e-target --privileged --network host \
        -v "$HOST_MODULES:/lib/modules:ro" -v /sys/kernel/config:/sys/kernel/config \
        -v e2e-modules:/modules:ro -v e2e-luns:/luns -e ISCSI_CONFIG="$(config "$1")" \
        "$TARGET_IMAGE" > /dev/null
    for i in $(seq 1 60); do
        docker exec e2e-target test -f /run/iscsicontainer/state.json && return 0
        [ "$(docker inspect -f '{{.State.Running}}' e2e-target)" = true ] || fail "target stopped"
        sleep 1
    done
    fail "target did not start"
}
param() {
    docker exec e2e-target cat "$TPG/param/MaxConnections"
}

cleanup
docker volume create e2e-modules > /dev/null
docker volume create e2e-luns > /dev/null
docker run --rm -v e2e-luns:/luns --entrypoint truncate "$TARGET_IMAGE" -s 64M /luns/e2e.img

echo "== module service"
docker run -d --name e2e-module -v "$HOST_MODULES:/host/lib/modules:ro" -v e2e-modules:/modules \
    "$MODULE_IMAGE" > /dev/null
for i in $(seq 1 900); do
    docker exec e2e-module iscsimodule health > /dev/null 2>&1 && break
    [ "$(docker inspect -f '{{.State.Running}}' e2e-module)" = true ] || fail "module service stopped"
    sleep 1
done
docker logs e2e-module 2>&1 | tail -3
status=$(docker exec e2e-module sh -c 'cat /modules/*/manifest.json' | sed 's/.*"status": "\([^"]*\)".*/\1/')
[ "$status" = "$EXPECT" ] || fail "module status $status, expected $EXPECT"

echo "== target without MC/S"
target 1
docker exec e2e-target iscsictl health || fail "not healthy with a single connection"
[ "$(param)" = 1 ] || fail "MaxConnections is not 1"

echo "== target with MC/S"
target 4
if [ "$EXPECT" = patched ] || [ "$EXPECT" = original-has-fix ]; then
    docker exec e2e-target iscsictl health || fail "not healthy with MC/S and status $EXPECT"
    [ "$(param)" = 4 ] || fail "MaxConnections is not 4"
else
    out=$(docker exec e2e-target iscsictl health) && fail "healthy although MC/S is ignored"
    echo "$out" | grep -q "MC/S is configured but ignored, module status $EXPECT" || fail "$out"
    [ "$(param)" = 1 ] || fail "MaxConnections is not forced to 1"
    docker logs e2e-target 2>&1 | grep -q "MC/S IS IGNORED" || fail "no warning in the log"
fi

echo "== stop clears the kernel"
docker stop -t 30 e2e-target > /dev/null
[ "$(docker inspect -f '{{.State.ExitCode}}' e2e-target)" = 0 ] || fail "target exit code"
docker run --rm -v /sys/kernel/config:/sys/kernel/config --entrypoint sh "$TARGET_IMAGE" \
    -c "! test -e /sys/kernel/config/target/iscsi/$IQN" || fail "target left in the kernel"
cleanup
echo "OK ($EXPECT)"
