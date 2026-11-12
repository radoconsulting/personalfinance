#!/bin/sh
# Runs centen as the owner of /data (your NAS user), so files keep their owner and permissions.
# PUID / PGID override that; without root (`user:` set in compose) it just runs as that user.
set -e
DATA="${CENTEN_HOME:-/data}"
if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA"
  uid="${PUID:-$(stat -c %u "$DATA")}"
  gid="${PGID:-$(stat -c %g "$DATA")}"
  if [ "$uid" = "0" ]; then
    echo "centen: /data belongs to root; running as root (set PUID/PGID to run as another user)"
    exec "$@"
  fi
  if ! setpriv --reuid="$uid" --regid="$gid" --clear-groups sh -c "touch '$DATA/.write-test' && rm -f '$DATA/.write-test' && for f in '$DATA'/*.db '$DATA'/config.json '$DATA'/*.pem; do [ ! -e \"\$f\" ] || { [ -r \"\$f\" ] && [ -w \"\$f\" ]; } || exit 1; done" 2>/dev/null; then
    echo "centen: user $uid:$gid can't write to $DATA; giving it ownership of the folder"
    chown -R "$uid:$gid" "$DATA" || true
    chmod -R u+rwX "$DATA" || true
  fi
  if setpriv --reuid="$uid" --regid="$gid" --clear-groups sh -c "touch '$DATA/.write-test' && rm -f '$DATA/.write-test' && for f in '$DATA'/*.db '$DATA'/config.json '$DATA'/*.pem; do [ ! -e \"\$f\" ] || { [ -r \"\$f\" ] && [ -w \"\$f\" ]; } || exit 1; done" 2>/dev/null; then
    exec setpriv --reuid="$uid" --regid="$gid" --clear-groups env HOME=/tmp "$@"
  fi
  echo "centen: $DATA still not writable for $uid:$gid (NAS permissions/ACLs); running as root instead"
  exec "$@"
fi
if ! touch "$DATA/.write-test" 2>/dev/null; then
  echo "centen: can't write to $DATA as user $(id -u):$(id -g)."
  echo "        Remove 'user:' from compose.yaml (centen then picks the folder's owner) or fix the folder's permissions."
  sleep 300
  exit 1
fi
rm -f "$DATA/.write-test"
exec "$@"
