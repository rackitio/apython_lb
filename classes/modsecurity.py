import asyncio
import ctypes
import ctypes.util
import logging
import os

from metrics import WAF_ENGINE_ENABLED, WAF_INSPECTIONS_TOTAL

logger = logging.getLogger(__name__)

MODSECURITY_ENABLED = os.environ.get("MODSECURITY_ENABLED", "false").lower() == "true"
MODSECURITY_RULES_FILE = os.environ.get("MODSECURITY_RULES_FILE", "/app/modsecurity.conf")
# When true, refuse to start (or serve a request) if the WAF was requested but
# cannot run, instead of silently continuing without inspection.
MODSECURITY_FAIL_CLOSED = os.environ.get("MODSECURITY_FAIL_CLOSED", "false").lower() == "true"

_vp = ctypes.c_void_p
_cp = ctypes.c_char_p
_ci = ctypes.c_int
_sz = ctypes.c_size_t


class _Intervention(ctypes.Structure):
    _fields_ = [
        ("status", ctypes.c_int),
        ("pause", ctypes.c_int),
        ("url", ctypes.c_char_p),
        ("log", ctypes.c_char_p),
        ("disruptive", ctypes.c_int),
    ]


def _load_lib():
    name = ctypes.util.find_library("modsecurity")
    if name:
        try:
            return ctypes.CDLL(name)
        except OSError:
            pass
    for soname in ("libmodsecurity.so.3", "libmodsecurity.so"):
        try:
            return ctypes.CDLL(soname)
        except OSError:
            continue
    return None


def _setup_lib(lib):
    lib.msc_init.restype, lib.msc_init.argtypes = _vp, []
    lib.msc_create_rules_set.restype, lib.msc_create_rules_set.argtypes = _vp, []
    lib.msc_rules_add_file.restype = _ci
    lib.msc_rules_add_file.argtypes = [_vp, _cp, ctypes.POINTER(_cp)]
    lib.msc_new_transaction.restype, lib.msc_new_transaction.argtypes = _vp, [_vp, _vp, _vp]
    lib.msc_process_connection.restype = _ci
    lib.msc_process_connection.argtypes = [_vp, _cp, _ci, _cp, _ci]
    lib.msc_process_uri.restype = _ci
    lib.msc_process_uri.argtypes = [_vp, _cp, _cp, _cp]
    lib.msc_add_n_request_header.restype = _ci
    lib.msc_add_n_request_header.argtypes = [_vp, _cp, _sz, _cp, _sz]
    lib.msc_process_request_headers.restype, lib.msc_process_request_headers.argtypes = _ci, [_vp]
    lib.msc_append_request_body.restype = _ci
    lib.msc_append_request_body.argtypes = [_vp, _cp, _sz]
    lib.msc_process_request_body.restype, lib.msc_process_request_body.argtypes = _ci, [_vp]
    lib.msc_intervention.restype = _ci
    lib.msc_intervention.argtypes = [_vp, ctypes.POINTER(_Intervention)]
    lib.msc_intervention_cleanup.restype = None
    lib.msc_intervention_cleanup.argtypes = [ctypes.POINTER(_Intervention)]
    lib.msc_transaction_cleanup.restype = None
    lib.msc_transaction_cleanup.argtypes = [_vp]


class ModSecurityEngine:
    """
    Async-compatible wrapper around libmodsecurity3, using ctypes to call the
    stable C API directly.  No compiled extension package required — only the
    libmodsecurity runtime shared library (.so.3) must be present.

    Runs all blocking C calls in a thread executor so they do not block the
    async event loop.  Instantiate once at app startup and attach to app.modsec.

    Environment variables:
        MODSECURITY_ENABLED      - set to "true" to activate (default: false)
        MODSECURITY_RULES_FILE   - path to the ModSecurity rules file
                                   (default: /app/modsecurity.conf)
        MODSECURITY_FAIL_CLOSED  - "true" to raise at startup (and reject
                                   requests) when the WAF was requested but
                                   cannot run, instead of continuing without
                                   inspection (default: false)
    """

    def __init__(
        self,
        rules_file: str = MODSECURITY_RULES_FILE,
        enabled: bool = MODSECURITY_ENABLED,
        fail_closed: bool = MODSECURITY_FAIL_CLOSED,
    ):
        self.enabled = False
        self.fail_closed = fail_closed
        WAF_ENGINE_ENABLED.set(0)

        if not enabled:
            logger.debug("ModSecurity WAF disabled (MODSECURITY_ENABLED=false)")
            return

        lib = _load_lib()
        if lib is None:
            self._init_failed("libmodsecurity shared library not found")
            return

        _setup_lib(lib)

        modsec_h = lib.msc_init()
        if not modsec_h:
            self._init_failed("msc_init() returned NULL")
            return

        rules_h = lib.msc_create_rules_set()
        if not rules_h:
            self._init_failed("msc_create_rules_set() returned NULL")
            return

        error_p = _cp()
        result = lib.msc_rules_add_file(rules_h, rules_file.encode(), ctypes.byref(error_p))
        if result < 0:
            err = error_p.value.decode() if error_p.value else "unknown error"
            raise RuntimeError(f"Failed to load ModSecurity rules from {rules_file!r}: {err}")

        self._lib = lib
        self._modsec = modsec_h
        self._rules = rules_h
        self.enabled = True
        WAF_ENGINE_ENABLED.set(1)
        logger.debug(f"ModSecurity engine initialised with rules from {rules_file!r}")

    def _init_failed(self, reason: str):
        """
        The WAF was requested (MODSECURITY_ENABLED=true) but cannot run.
        Loud by design: a silently absent WAF is an outage an operator only
        discovers during an attack.
        """
        if self.fail_closed:
            raise RuntimeError(f"{reason} — refusing to start (MODSECURITY_FAIL_CLOSED=true)")
        logger.error(f"{reason} — WAF DISABLED, requests will NOT be inspected")

    def _inspect_sync(
        self,
        client_ip: str,
        method: str,
        uri: str,
        headers: dict,
        body: bytes,
    ) -> tuple[bool, int]:
        lib = self._lib
        txn = lib.msc_new_transaction(self._modsec, self._rules, None)
        if not txn:
            if self.fail_closed:
                logger.error("msc_new_transaction() returned NULL — rejecting request")
                return False, 500
            logger.warning("msc_new_transaction() returned NULL — allowing request uninspected")
            return True, 200

        try:
            lib.msc_process_connection(txn, client_ip.encode(), 0, b"", 0)
            lib.msc_process_uri(txn, uri.encode(), method.encode(), b"1.1")

            for name, value in headers.items():
                nb = name.encode("utf-8", errors="replace") if isinstance(name, str) else name
                vb = value.encode("utf-8", errors="replace") if isinstance(value, str) else value
                lib.msc_add_n_request_header(txn, nb, len(nb), vb, len(vb))

            lib.msc_process_request_headers(txn)

            if body:
                lib.msc_append_request_body(txn, body, len(body))
            lib.msc_process_request_body(txn)

            it = _Intervention()
            it.status = 200
            if lib.msc_intervention(txn, ctypes.byref(it)):
                status = it.status or 403
                lib.msc_intervention_cleanup(ctypes.byref(it))
                return False, status
            lib.msc_intervention_cleanup(ctypes.byref(it))

            return True, 200
        finally:
            lib.msc_transaction_cleanup(txn)

    async def inspect_request(
        self,
        client_ip: str,
        method: str,
        uri: str,
        headers: dict,
        body: bytes,
    ) -> tuple[bool, int]:
        """
        Inspect an HTTP request through the ModSecurity WAF engine.
        Returns (allowed, http_status_code).
        """
        if not self.enabled:
            return True, 200

        loop = asyncio.get_running_loop()
        allowed, status = await loop.run_in_executor(
            None,
            lambda: self._inspect_sync(client_ip, method, uri, headers, body),
        )
        WAF_INSPECTIONS_TOTAL.labels(action="allowed" if allowed else "blocked").inc()
        return allowed, status
