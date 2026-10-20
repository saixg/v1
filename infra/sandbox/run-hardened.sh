#!/usr/bin/env sh
# Run the API image under the Docker hardening described in
# infra/sandbox/README.md. This is the self-hosted / local-verification path;
# on Fly the microVM supplies the boundary and these flags do not exist
# (see fly.toml's note where [mounts] would be).
#
# Usage, from the workspace root:
#   docker build -f services/api/Dockerfile -t codeaxis-api .
#   infra/sandbox/run-hardened.sh --env-file .env
#
# Any extra arguments are passed through to `docker run`, which is how secrets
# get in: --env-file, or repeated --env. Nothing credential-bearing is baked
# into the image.
set -eu

IMAGE="${CODEAXIS_IMAGE:-codeaxis-api}"

exec docker run --rm \
  --name codeaxis-api \
  --publish 8000:8080 \
  \
  `# The image already declares USER 10001; restated so a wrong --user in an` \
  `# operator's own wrapper is visible here rather than implicit.` \
  --user 10001:10001 \
  \
  `# The process reads untrusted repository content. It never needs to write` \
  `# outside scratch, so everything else is immutable at run time.` \
  --read-only \
  \
  `# The only writable path. noexec is the load-bearing flag: a checked-out` \
  `# repository lands here, and nosuid/noexec/nodev means nothing in it can be` \
  `# executed even by accident. Sized above ScanLimits' 2 GB total-repo cap.` \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=3g,mode=1777 \
  \
  `# git and uvicorn need no capabilities at all. Dropping ALL and adding none` \
  `# back is the whole capability set this service requires.` \
  --cap-drop ALL \
  \
  `# Blocks setuid/setgid escalation and the ptrace/file-capability paths that` \
  `# a dropped-capability process could otherwise regain privileges through.` \
  --security-opt no-new-privileges \
  \
  `# Bounds a parse bomb. The per-file parse timeout in` \
  `# app/analysis/scan/limits.py abandons its worker thread rather than` \
  `# killing it (see README), so wall-clock enforcement is here: a runaway` \
  `# thread cannot take more than two cores or exhaust host RAM.` \
  --cpus 2 \
  --memory 4g \
  --memory-swap 4g \
  --pids-limit 512 \
  \
  "$@" \
  "$IMAGE"
