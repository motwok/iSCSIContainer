#!/bin/sh
# Writes the installed Debian packages of both images to packages/, one file per image.
# Usage: tools/packages.sh [platform ...]   (default: linux/amd64 linux/arm64)
set -eu
cd "$(dirname "$0")/.."
[ "$#" -gt 0 ] || set -- linux/amd64 linux/arm64
mkdir -p packages
for image in target module; do
    case "$image" in
        target) file=Dockerfile ;;
        module) file=module/Dockerfile ;;
    esac
    out="packages/$image.txt"
    : > "$out"
    for platform in "$@"; do
        tag="iscsicontainer-packages-$image"
        docker buildx build --quiet --load --platform "$platform" -f "$file" -t "$tag" . > /dev/null
        echo "# $platform" >> "$out"
        docker run --rm --platform "$platform" --entrypoint dpkg-query "$tag" \
            -W -f '${Package} ${Version}\n' | LC_ALL=C sort >> "$out"
    done
done
