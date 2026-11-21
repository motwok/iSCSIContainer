#!/bin/sh
# Module service and target in a Debian 13 VM: the module the service builds for the Debian kernel
# is loaded by the target from its cache (status patched, MC/S allowed). Needs KVM, qemu, Docker
# and cloud-localds on the host; runs tests/e2e/run.sh inside the VM.
# TARGET_IMAGE, MODULE_IMAGE: images to test (loaded from the host's Docker).
set -eu
TARGET_IMAGE=${TARGET_IMAGE:-iscsicontainer:test}
MODULE_IMAGE=${MODULE_IMAGE:-iscsicontainer-module:test}
IMAGE_URL=${IMAGE_URL:-https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2}
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
WORK=${WORK:-/tmp/iscsi-vm}
PORT=2222
mkdir -p "$WORK"
cd "$WORK"

vm() {
    ssh -q -i key -p "$PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ConnectTimeout=5 debian@127.0.0.1 "$@"
}
fail() {
    echo "FAILED: $1"
    tail -40 serial.log 2> /dev/null || true
    exit 1
}

[ -f base.qcow2 ] || curl -fsSL -o base.qcow2 "$IMAGE_URL"
rm -f disk.qcow2 seed.img key key.pub
qemu-img create -q -f qcow2 -F qcow2 -b base.qcow2 disk.qcow2 12G
ssh-keygen -q -t ed25519 -N '' -f key
cat > user-data <<EOF
#cloud-config
users:
  - name: debian
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - $(cat key.pub)
EOF
printf 'instance-id: iscsi-vm\nlocal-hostname: iscsi-vm\n' > meta-data
cloud-localds seed.img user-data meta-data

qemu-system-x86_64 -enable-kvm -cpu host -smp 4 -m 4096 \
    -drive file=disk.qcow2,if=virtio -drive file=seed.img,if=virtio,format=raw \
    -nic user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:$PORT-:22 \
    -display none -serial file:serial.log -daemonize -pidfile qemu.pid

i=0
until vm true 2> /dev/null; do
    i=$((i + 1))
    [ "$i" -le 120 ] || fail "the VM does not answer over SSH"
    sleep 2
done
echo "VM kernel: $(vm uname -r)"
vm 'cloud-init status --wait > /dev/null 2>&1 || true'
vm 'sudo apt-get update -q > /dev/null && sudo apt-get install -y -q docker.io > /dev/null' ||
    fail "Docker could not be installed in the VM"
docker save "$TARGET_IMAGE" "$MODULE_IMAGE" | vm 'sudo docker load' > /dev/null ||
    fail "the images could not be copied into the VM"
vm 'mkdir -p e2e' && vm 'cat > e2e/run.sh' < "$ROOT/tests/e2e/run.sh"
vm "sudo TARGET_IMAGE='$TARGET_IMAGE' MODULE_IMAGE='$MODULE_IMAGE' EXPECT=patched sh e2e/run.sh" ||
    fail "end-to-end test in the VM"
vm 'sudo lsmod | grep "^iscsi_target_mod" && cat /sys/module/iscsi_target_mod/srcversion'
kill "$(cat qemu.pid)" 2> /dev/null || true
echo "OK (Debian VM)"
