"""Outbound-only Netflow job transport. It never accepts student credentials."""
from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import os
import re
import ssl
import threading
from urllib.parse import urlsplit


_LOG = logging.getLogger(__name__)
_CLAIM = "/api/integration/jobs/claim"
_STATUS = "/api/integration/jobs/status"
_MAX_BYTES = 16384
_ID = re.compile(r"[0-9a-f]{32}\Z")
_ASSIGNMENT = re.compile(r"lab-[1-9][0-9]{0,9}-[1-9][0-9]{0,8}\Z")
_LEASE = re.compile(r"[A-Za-z0-9_-]{16,180}\Z")
_STATES = {"queued", "building", "awaiting_confirmation", "ready", "stopped", "expired", "error", "rejected", "removed"}


class NetflowExchangeError(Exception):
    """Safe to log: no headers, response bodies, user identifiers or credentials."""


def _origin(value):
    # Parse an origin, never a free-form URL or a URL supplied in a claim.
    if (not isinstance(value, str) or len(value) > 300 or not value.isascii()
            or not re.fullmatch(r"https://[A-Za-z0-9.\-:\[\]]+/?", value)):
        raise ValueError("NETFLOW_INTEGRATION_URL must be an HTTPS origin")
    try:
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
        if not host or parts.netloc.endswith(":") or port == 0:
            raise ValueError
        if parts.netloc.startswith("["):
            ipaddress.IPv6Address(host)
            if not re.fullmatch(r"\[[A-Fa-f0-9:]+\](?::[0-9]{1,5})?", parts.netloc):
                raise ValueError
        elif (not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?", parts.netloc)
              or len(host) > 253 or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                                          for label in host.split("."))):
            raise ValueError
    except ValueError:
        raise ValueError("NETFLOW_INTEGRATION_URL must be an HTTPS origin") from None
    return host, port or 443


def _unique_fields(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON keys")
    return result


class NetflowClient:
    def __init__(self, origin, token):
        self._host, self._port = _origin(origin)
        if (not isinstance(token, str) or not re.fullmatch(r"[\x21-\x7e]{32,256}", token)):
            raise ValueError("NETFLOW_WORKER_TOKEN must be 32-256 visible ASCII characters")
        self._token = token
        self._tls = ssl.create_default_context()

    def _post(self, path, payload):
        assert path in (_CLAIM, _STATUS)
        # Deliberately no system HTTP proxy, cookie jar, redirect support or HTTP fallback.
        conn = http.client.HTTPSConnection(self._host, self._port, context=self._tls, timeout=8)
        try:
            conn.request("POST", path, body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                         headers={"Authorization": "Bearer " + self._token,
                                  "Accept": "application/json", "Content-Type": "application/json"})
            res = conn.getresponse()
            if path == _STATUS and res.status == 204:
                return {"ok": True}
            if res.status != 200:
                raise NetflowExchangeError(f"Netflow HTTP {int(res.status)}")
            media = res.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            if media != "application/json" or res.getheader("Content-Encoding", "identity").lower() != "identity":
                raise NetflowExchangeError("unexpected Netflow response format")
            if res.length is not None and res.length > _MAX_BYTES:
                raise NetflowExchangeError("oversized Netflow response")
            raw = res.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                raise NetflowExchangeError("oversized Netflow response")
            obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields)
            if not isinstance(obj, dict):
                raise NetflowExchangeError("invalid Netflow JSON object")
            return obj
        except NetflowExchangeError:
            raise
        except (OSError, http.client.HTTPException, ValueError, RecursionError):
            raise NetflowExchangeError("Netflow HTTPS/JSON exchange failed") from None
        finally:
            conn.close()

    def claim(self):
        envelope = self._post(_CLAIM, {})
        if set(envelope) != {"job"}:
            raise NetflowExchangeError("invalid Netflow claim")
        job = envelope["job"]
        if job is None:
            return None
        keys = {"job_id", "subject", "assignment_id", "group", "lease_token"}
        if (not isinstance(job, dict) or set(job) not in (keys, keys | {"action"})
                or ("action" in job and job["action"] != "reconcile")):
            raise NetflowExchangeError("invalid Netflow claim")
        for field, pattern in (("job_id", _ID), ("subject", _ID), ("assignment_id", _ASSIGNMENT), ("lease_token", _LEASE)):
            if not isinstance(job[field], str) or not pattern.fullmatch(job[field]):
                raise NetflowExchangeError("invalid Netflow claim")
        group = job["group"]
        if not isinstance(group, str) or not group.strip() or len(group) > 60 or not group.isprintable():
            raise NetflowExchangeError("invalid Netflow claim")
        return job

    def publish(self, job, result, *, allow_links):
        if not isinstance(result, dict):
            raise NetflowExchangeError("invalid local lab status")
        status = result.get("status")
        progress = result.get("progress")
        if not isinstance(status, str) or status not in _STATES or type(progress) is not int or not 0 <= progress <= 100:
            raise NetflowExchangeError("invalid local lab status")
        if "action" in job and (job["action"] != "reconcile" or status not in ("error", "removed")):
            raise NetflowExchangeError("invalid access reconciliation result")
        # An allowlist, not dict(result): raw gateway output may contain a guest password.
        msg = {"job_id": job["job_id"], "lease_token": job["lease_token"], "status": status, "progress": progress}
        if allow_links and status == "ready":
            url = result.get("web_url")
            if isinstance(url, str):
                match = re.fullmatch(r"https://(10\.39\.[0-9]{1,3}\.[0-9]{1,3}):8006/", url)
                if match:
                    try:
                        ip = ipaddress.IPv4Address(match[1])
                    except ValueError:
                        ip = None
                    if ip and str(ip) not in {"10.39.0.0", "10.39.255.255"}:
                        msg["web_url"] = url
        if self._post(_STATUS, msg).get("ok") is not True:
            raise NetflowExchangeError("invalid Netflow status acknowledgement")


class NetflowWorker:
    def __init__(self, client, adapter, *, allow_links=False):
        self._client, self._adapter = client, adapter
        self._allow_links = allow_links
        self._stop = threading.Event()
        self._thread = threading.Thread(name="netflow-outbound", target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=10)

    def run_once(self):
        job = self._client.claim()
        if job is None or self._stop.is_set():
            return False
        result = self._adapter.handle(job)
        self._client.publish(job, result, allow_links=self._allow_links)
        return True

    def _run(self):
        retry = 0
        while not self._stop.is_set():
            try:
                worked = self.run_once()
                if retry:
                    _LOG.info("Соединение с очередью Netflow восстановлено.")
                retry = 0
                delay = 0.15 if worked else 5
            except Exception:
                # The local adapter may propagate an opaque service failure after a commit;
                # never fabricate a terminal result or log the response/claim. Redelivery is
                # fenced at Netflow and the durable receipt is checked again by the adapter.
                if not retry:
                    _LOG.warning("Обмен с очередью Netflow приостановлен; повторим автоматически.")
                retry = min(60, retry * 2 if retry else 5)
                delay = retry
            self._stop.wait(delay)


def start_netflow_worker(service, labs, env=None):
    env = os.environ if env is None else env
    url, token = env.get("NETFLOW_INTEGRATION_URL", ""), env.get("NETFLOW_WORKER_TOKEN", "")
    links = env.get("NETFLOW_ALLOW_LAB_LINKS", "0")
    if not url and not token and links == "0":
        return None
    if not url or not token or links not in ("0", "1"):
        raise ValueError("Netflow requires both URL and worker token, and NETFLOW_ALLOW_LAB_LINKS=0 or 1")
    if any(env.get(name) == token for name in ("DASHBOARD_ADMIN_TOKEN", "PROXMOX_TOKEN_VALUE")):
        raise ValueError("NETFLOW_WORKER_TOKEN must be separate from the dashboard and Proxmox credentials")
    if labs is None or service.gateway.mode != "live":
        raise ValueError("Netflow worker requires live student labs")
    # A worker with a missing/invalid student IP range would continuously
    # accept leases only to fail every submission; diagnose this before polling.
    from .student_errors import StudentError
    try:
        labs._range()
    except StudentError:
        raise ValueError("Netflow worker requires a valid STUDENT_LAB_IP_START/END range") from None
    from .netflow_labs import NetflowLabs
    worker = NetflowWorker(NetflowClient(url, token), NetflowLabs(service, labs), allow_links=links == "1")
    worker.start()
    return worker
