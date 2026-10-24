#!/usr/bin/env bash
set -euo pipefail

case "${TOOLBOX_DOCKER:-0}" in
    0) exec "$@" ;;
    1) ;;
    *) echo 'TOOLBOX_DOCKER must be 0 or 1' >&2; exit 1 ;;
esac

if [[ $(id -u) != 0 ]]; then
    echo 'Local Docker startup requires root in a Docker-capable sandbox' >&2
    exit 1
fi
if [[ ${DOCKER_HOST:-unix:///var/run/docker.sock} != unix:///var/run/docker.sock ]]; then
    echo 'Unset TOOLBOX_DOCKER when using an external DOCKER_HOST' >&2
    exit 1
fi
export DOCKER_HOST=unix:///var/run/docker.sock
unset DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH

if docker info >/dev/null 2>&1; then
    exec "$@"
fi

log=$(mktemp /tmp/toolbox-dockerd.XXXXXX)
daemon_pid=
command_pid=
# Invoked by the EXIT trap.
# shellcheck disable=SC2329
cleanup() {
    if [[ -n $command_pid ]]; then
        kill "$command_pid" 2>/dev/null || true
        wait "$command_pid" 2>/dev/null || true
    fi
    if [[ -n $daemon_pid ]]; then
        kill "$daemon_pid" 2>/dev/null || true
        wait "$daemon_pid" 2>/dev/null || true
    fi
    rm -f "$log"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

dockerd --host="$DOCKER_HOST" --exec-opt=native.cgroupdriver=cgroupfs >"$log" 2>&1 &
daemon_pid=$!
ready=0
for ((attempt = 0; attempt < 30; attempt++)); do
    if ! kill -0 "$daemon_pid" 2>/dev/null; then
        break
    fi
    if docker info >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done
if [[ $ready != 1 ]]; then
    echo 'Docker did not become ready within 30 seconds; CI command not started' >&2
    cat "$log" >&2
    exit 1
fi

echo "Docker ready; daemon log: $log" >&2
"$@" <&0 &
command_pid=$!
status=0
wait "$command_pid" || status=$?
command_pid=
exit "$status"
