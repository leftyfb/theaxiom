#!/usr/bin/env bash
set -euo pipefail

overwrite=false
if [[ $# -eq 5 && $5 == --overwrite ]]; then
  overwrite=true
  set -- "${@:1:4}"
fi
if [[ $# -ne 4 ]]; then
  echo "usage: $0 URL DESTINATION EXPECTED_BYTES EXPECTED_SHA256 [--overwrite]" >&2
  exit 64
fi

url=$1
destination=$2
expected_bytes=$3
expected_sha256=${4,,}
staging_dir=${THEAXIOM_STAGING_DIR:-${XDG_DATA_HOME:-"$HOME/.local/share"}/theaxiom/staging}

[[ $url == https://* ]] || { echo "URL must use HTTPS" >&2; exit 64; }
[[ $destination == "$staging_dir"/* ]] || {
  echo "destination must be under $staging_dir" >&2
  exit 64
}
[[ $expected_bytes =~ ^[0-9]+$ ]] || { echo "invalid expected byte count" >&2; exit 64; }
[[ $expected_sha256 =~ ^[0-9a-f]{64}$ ]] || { echo "invalid expected SHA-256" >&2; exit 64; }

mkdir -p -- "$(dirname -- "$destination")"
partial="${destination}.part"
lock="${destination}.lock"
exec 9>"$lock"
flock -n 9 || { echo "another acquisition is already running" >&2; exit 75; }

verify_file() {
  local file=$1 actual_bytes actual_sha256
  actual_bytes=$(stat -c '%s' -- "$file")
  [[ $actual_bytes == "$expected_bytes" ]] || {
    echo "size mismatch: expected $expected_bytes, got $actual_bytes" >&2
    return 1
  }
  actual_sha256=$(sha256sum -- "$file" | cut -d' ' -f1)
  [[ $actual_sha256 == "$expected_sha256" ]] || {
    echo "SHA-256 mismatch" >&2
    return 1
  }
}

if [[ -f $destination ]]; then
  if [[ $overwrite != true ]]; then
    verify_file "$destination"
    echo "already complete: $destination"
    exit 0
  fi
elif [[ -e $destination || -L $destination ]]; then
  echo "destination exists but is not a regular file" >&2
  exit 1
fi

if [[ $overwrite == true ]]; then
  partial=$(mktemp "$(dirname -- "$destination")/.acquire-kiwix.XXXXXXXXXX")
  cleanup() { rm -f -- "$partial"; }
  trap cleanup EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
fi

curl --fail --location --retry 8 --retry-all-errors \
  --continue-at - --output "$partial" "$url"
verify_file "$partial"
mv -f -- "$partial" "$destination"
echo "verified and installed: $destination"