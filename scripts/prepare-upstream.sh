#!/bin/sh
# Prepare an existing checkout for testing or installation.
set -eu
if [ "$#" -ne 1 ]; then
    echo "Usage: $0 PATH_TO_XINCHAO_NIAN_CHECKOUT" >&2
    exit 2
fi
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
target=$(CDPATH='' cd -- "$1" && pwd)
pin=444e7db2d138ad7ff23cafd774a5c0d3eb37aae5
if [ "$(git -C "$target" rev-parse HEAD)" != "$pin" ]; then
    echo "Expected xinchao-nian at $pin" >&2
    exit 1
fi
apply_patch_if_needed() {
    patch=$1
    if git -C "$target" apply --reverse --check "$repo/$patch" 2>/dev/null; then
        echo "$patch already applied"
    else
        git -C "$target" apply --check "$repo/$patch"
        git -C "$target" apply "$repo/$patch"
    fi
}
apply_patch_if_needed upstream.patch
# The window upgrade changes lines introduced by the original dream patch.
if ! git -C "$target" apply --reverse --check "$repo/aru-dream-window.patch" 2>/dev/null; then
    apply_patch_if_needed aru-dream.patch
    apply_patch_if_needed aru-dream-window.patch
fi
apply_patch_if_needed restless-wording.patch
for module in aru_backup aru_backup_ob aru_enrichment aru_source_time aru_wikilinks nian_memory; do
    install -m 0644 "$repo/$module.py" "$target/ombre-brain/src/$module.py"
done
