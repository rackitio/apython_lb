#!/usr/bin/env bash
# Smoke-tests for the ModSecurity WAF.
# Requires the container to be running locally:
#
#   docker build --target final -t apython_lb:local .
#   docker run --rm -p 8443:443 \
#     -e MODSECURITY_ENABLED=true \
#     -v "$PWD/certs/cert.pem":/app/cert.pem:ro \
#     -v "$PWD/certs/key.pem":/app/key.pem:ro \
#     apython_lb:local
#
# Usage: bash tests/smoke_waf.sh [host:port]
# Default host: https://localhost:8443

set -uo pipefail

BASE="${1:-https://localhost:8443}"
PASS=0
FAIL=0

check() {
    local desc="$1"
    local expected="$2"
    shift 2
    local actual
    actual=$(curl -sk -o /dev/null -w "%{http_code}" "$@")
    if [[ "$actual" == "$expected" ]]; then
        echo "PASS  [$expected] $desc"
        PASS=$((PASS + 1))
    else
        echo "FAIL  [got $actual, want $expected] $desc"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== WAF smoke tests → $BASE ==="
echo

# ── Clean requests (WAF should pass; 503 = no backend configured, not a WAF block) ──
check "health endpoint passes WAF"         200 "$BASE/health"
check "clean GET passes WAF"               503 "$BASE/test"

# ── Phase 1: URI / headers ────────────────────────────────────────────────────
# null byte: %00 in URI path
check "null byte in URI blocked"           400 "$BASE/test%00.php"
# path traversal: test via query arg (curl normalises ../ in URL paths before sending)
check "path traversal in query arg blocked" 400 \
    -G --data-urlencode "file=../../etc/passwd" "$BASE/test"
# URI length: rule 910004 fires at >2048 chars
check "oversized URI blocked"              414 \
    "$BASE/$(python3 -c 'print("a"*2050)')"
# scanner UAs
check "sqlmap User-Agent blocked"          403 -A "sqlmap/1.7.8" "$BASE/test"
check "nikto User-Agent blocked"           403 -A "Mozilla/5.00 (Nikto/2.1.6)" "$BASE/test"
# SQLi and XSS via libinjection in query string
check "SQLi in query string blocked"       403 \
    -G --data-urlencode "q=1' OR '1'='1" "$BASE/search"
check "XSS in query string blocked"        403 \
    -G --data-urlencode "q=<script>alert(1)</script>" "$BASE/search"

# ── Phase 2: request body ─────────────────────────────────────────────────────
# Note: POST to /<path:path> (not root /) so the route accepts the method.
check "SQLi in POST body blocked"          403 \
    -X POST -d "input=1' UNION SELECT username,password FROM users--" "$BASE/test"
check "XSS in POST body blocked"           403 \
    -X POST -d "comment=<img src=x onerror=alert(1)>" "$BASE/test"
check "remote file inclusion blocked"      403 \
    -X POST -d "url=http://evil.example.com/shell.php" "$BASE/test"

# ── Note ──────────────────────────────────────────────────────────────────────
# Rule 910003 (Transfer-Encoding smuggling) is intentionally not tested here.
# Hypercorn processes Transfer-Encoding at the transport layer before the ASGI
# app sees the request, so the header never reaches the application-layer WAF.

echo
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ $FAIL -eq 0 ]]
