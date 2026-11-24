#!/bin/bash
set -euo pipefail
umask 077

signing_dir="$HOME/Library/Application Support/SeekBar Signing"
keychain="$signing_dir/identity.keychain-db"
if [[ -s "$signing_dir/fingerprint" && -f "$keychain" && -s "$signing_dir/keychain-password" ]]; then
    printf 'Using the existing SeekBar signing identity.\n'
    exit 0
fi
if ! mkdir -m 700 "$signing_dir"; then
    printf 'The existing SeekBar signing directory is incomplete. Restore its original identity before signing an update.\n' >&2
    exit 1
fi

scratch="$(mktemp -d -t seekbar-signing)"
trap 'rm -rf -- "$scratch"' EXIT
/usr/bin/openssl rand -hex 32 > "$signing_dir/keychain-password"
/usr/bin/openssl rand -hex 32 > "$scratch/pkcs-password"
keychain_secret="$(<"$signing_dir/keychain-password")"
/usr/bin/openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days 3650 \
    -subj '/CN=SeekBar Local Code Signing/' \
    -addext 'basicConstraints=critical,CA:FALSE' \
    -addext 'keyUsage=critical,digitalSignature' \
    -addext 'extendedKeyUsage=codeSigning' \
    -keyout "$scratch/private.pem" -out "$scratch/certificate.pem"
/usr/bin/openssl pkcs12 -export -inkey "$scratch/private.pem" -in "$scratch/certificate.pem" \
    -out "$scratch/identity.p12" -passout "file:$scratch/pkcs-password"
/usr/bin/security create-keychain -p "$keychain_secret" "$keychain"
/usr/bin/security unlock-keychain -p "$keychain_secret" "$keychain"
/usr/bin/security import "$scratch/identity.p12" -k "$keychain" -f pkcs12 \
    -P "$(<"$scratch/pkcs-password")" -T /usr/bin/codesign -x
/usr/bin/security set-key-partition-list -S apple-tool:,apple:,codesign: -s -t private \
    -k "$keychain_secret" "$keychain" >/dev/null
/usr/bin/security lock-keychain "$keychain"
/usr/bin/openssl x509 -in "$scratch/certificate.pem" -noout -fingerprint -sha1 | cut -d= -f2 | tr -d : > "$signing_dir/fingerprint"
printf 'Created the SeekBar-only signing identity.\n'
