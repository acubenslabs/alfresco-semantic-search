#!/usr/bin/env bash
# Generate config/activemq.xml with the STOMP connector enabled.
#
# The alfresco/alfresco-activemq image ships with only the openwire connector
# active — amqp, stomp, mqtt and ws are commented out upstream "to reduce the
# exposed attack surface". The Event2 listener in this project speaks STOMP,
# so it needs one of them turned on.
#
# This extracts the image's own activemq.xml and uncomments just the STOMP
# line, so the result stays in step with whatever version the image ships.
#
# Usage:  scripts/enable-stomp.sh [image]
set -euo pipefail

IMAGE="${1:-docker.io/alfresco/alfresco-activemq:6.2.9-jre17-rockylinux8}"
OUT="$(cd "$(dirname "$0")/.." && pwd)/config/activemq.xml"
mkdir -p "$(dirname "$OUT")"

echo "Extracting activemq.xml from ${IMAGE}"
CID=$(docker create "$IMAGE")
trap 'docker rm -f "$CID" >/dev/null 2>&1 || true' EXIT
docker cp "$CID":/opt/activemq/conf/activemq.xml "$OUT" >/dev/null

python3 - "$OUT" <<'PY'
import re, sys, pathlib
path = pathlib.Path(sys.argv[1])
text = path.read_text()
pattern = re.compile(r'<!--\s*(<transportConnector name="stomp"[^>]*/>)\s*-->')
patched, count = pattern.subn(r'\1', text)
if count:
    path.write_text(patched)
    print("STOMP connector enabled")
elif 'name="stomp"' in text and '<!--' not in text.split('name="stomp"')[0][-80:]:
    print("STOMP connector already enabled")
else:
    sys.exit("could not find the STOMP connector line; check the image version")
PY

echo "Wrote ${OUT}"
echo "Now run: docker compose up -d"
