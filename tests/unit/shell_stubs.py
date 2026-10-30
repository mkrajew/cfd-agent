"""Shared helpers for the tests that run the VM shell scripts with stub commands."""

import shutil
import stat
from pathlib import Path

BASH = shutil.which("bash")
PYTHON3 = shutil.which("python3")
SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "vm" / "idle-shutdown.sh"

NOW = 1_800_000_000
BOOT_AGO = 2 * 3600  # the stubbed VM booted two hours ago
COMPUTE_JSON = '{"subscriptionId":"s","resourceGroupName":"g","name":"v"}'
TOKEN_JSON = '{"access_token":"secret-token"}'

# Stub for curl: logs its arguments, writes a body to -o, prints the status code for -w and
# exits 22 on HTTP errors like `curl --fail-with-body`. The URL is the last argument.
CURL_STUB = r"""
echo "$*" >> "$STUB_CURL_LOG"
out=
prev=
url=
for arg in "$@"; do
  if [ "$prev" = "-o" ]; then out=$arg; fi
  prev=$arg
  url=$arg
done
code=200
body='{}'
case "$url" in
  *instance/compute*)
    code=${STUB_META_CODE:-200}; body='__COMPUTE__'
    if [ -n "$STUB_COMPUTE_FILE" ]; then body=$(cat "$STUB_COMPUTE_FILE"); fi ;;
  *oauth2/token*) code=${STUB_TOKEN_CODE:-200}; body='__TOKEN__' ;;
  */deallocate*)
    n=$(cat "$STUB_COUNTER" 2>/dev/null || echo 0)
    echo $((n + 1)) > "$STUB_COUNTER"
    if [ "$n" -lt "${STUB_ARM_FAILS:-0}" ]; then
      code=500; body='{"error":"boom"}'
    else
      code=202; body=''
    fi ;;
  *virtualMachines/v\?*) code=${STUB_GET_CODE:-200}; body='{"name":"v"}' ;;
esac
if [ -n "$out" ]; then printf '%s' "$body" > "$out"; fi
printf '%s' "$code"
if [ "$code" -ge 400 ]; then exit 22; fi
""".replace("__COMPUTE__", COMPUTE_JSON).replace("__TOKEN__", TOKEN_JSON)

PGREP_STUB = r"""
case "$*" in
  *-x*) exit "${STUB_PGREP_X_RC:-1}" ;;
  *) exit "${STUB_PGREP_F_RC:-1}" ;;
esac
"""


def stub_executable(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
