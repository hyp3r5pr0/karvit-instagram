"""Thin, careful client for the Instagram API with Instagram Login.

Docs: https://developers.facebook.com/docs/instagram-platform/content-publishing

Design notes
- The access token is sent in the POST body (never the URL) for writes, and is
  redacted from every error message, because Actions logs are public on a
  public repo.
- Reads and container creation retry on transient errors. The final
  ``media_publish`` call is NOT blindly retried (that is how duplicate posts
  happen); it only retries on Meta's explicit "media not ready" errors.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable

import requests

GRAPH_HOST = "https://graph.instagram.com"
RUPLOAD_HOST = "https://rupload.facebook.com/ig-api-upload"
DEFAULT_VERSION = "v25.0"

# Error codes Meta documents as temporary / throttling.
TRANSIENT_CODES = {1, 2, 4, 17, 32, 341, 613}
# "Media not ready for publishing" — safe to wait and retry media_publish.
NOT_READY_CODES = {9007}
NOT_READY_SUBCODES = {2207027}


class InstagramError(Exception):
    """API error with Meta's code/subcode attached and the token redacted."""

    def __init__(self, message: str, *, code: int | None = None,
                 subcode: int | None = None, http_status: int | None = None,
                 payload: Any = None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.http_status = http_status
        self.payload = payload

    @property
    def transient(self) -> bool:
        return (self.code in TRANSIENT_CODES
                or (self.http_status is not None and self.http_status >= 500))

    @property
    def not_ready(self) -> bool:
        return self.code in NOT_READY_CODES or self.subcode in NOT_READY_SUBCODES

    @property
    def token_problem(self) -> bool:
        return self.code == 190


class InstagramClient:
    def __init__(self, token: str, *, version: str = DEFAULT_VERSION,
                 session: requests.Session | None = None,
                 poll_interval: float = 15, poll_timeout: float = 900,
                 sleep: Callable[[float], None] = time.sleep,
                 max_retries: int = 3):
        if not token:
            raise InstagramError("IG_ACCESS_TOKEN is empty. Add it as a GitHub secret "
                                 "(see README, step 4).")
        self.token = token
        self.version = version
        self.http = session or requests.Session()
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self.sleep = sleep
        self.max_retries = max_retries

    # ------------------------------------------------------------------ core
    def _redact(self, text: str) -> str:
        return text.replace(self.token, "***") if self.token else text

    def _url(self, path: str) -> str:
        return f"{GRAPH_HOST}/{self.version}/{path.lstrip('/')}"

    def _call(self, method: str, url: str, *, params: dict | None = None,
              data: dict | None = None, headers: dict | None = None,
              body: bytes | None = None, timeout: float = 60) -> dict:
        try:
            if body is not None:
                resp = self.http.request(method, url, headers=headers, data=body,
                                         timeout=timeout)
            else:
                resp = self.http.request(method, url, params=params, data=data,
                                         headers=headers, timeout=timeout)
        except requests.RequestException as exc:  # network trouble
            raise InstagramError(f"Network error: {self._redact(str(exc))}",
                                 http_status=599) from None
        try:
            payload = resp.json()
        except ValueError:
            payload = {"raw": resp.text[:500]}
        if resp.status_code >= 400 or (isinstance(payload, dict) and "error" in payload):
            err = payload.get("error", {}) if isinstance(payload, dict) else {}
            msg = err.get("error_user_msg") or err.get("message") or str(payload)[:300]
            raise InstagramError(
                self._redact(f"Instagram API error (HTTP {resp.status_code}, code "
                             f"{err.get('code')}/{err.get('error_subcode')}): {msg}"),
                code=err.get("code"), subcode=err.get("error_subcode"),
                http_status=resp.status_code, payload=payload)
        return payload

    def _with_retry(self, fn: Callable[[], dict], *, retry_not_ready: bool = False,
                    retry_transient: bool = True) -> dict:
        delay = 5.0
        for attempt in range(self.max_retries + 1):
            try:
                return fn()
            except InstagramError as exc:
                retryable = (retry_transient and exc.transient) or (retry_not_ready and exc.not_ready)
                if not retryable or attempt == self.max_retries:
                    raise
                self.sleep(delay)
                delay *= 3
        raise AssertionError("unreachable")

    def get(self, path: str, **params) -> dict:
        params = {**params, "access_token": self.token}
        return self._with_retry(lambda: self._call("GET", self._url(path), params=params))

    def post(self, path: str, *, retry_transient: bool = True,
             retry_not_ready: bool = False, **fields) -> dict:
        data = {k: v for k, v in fields.items() if v is not None}
        data["access_token"] = self.token
        return self._with_retry(lambda: self._call("POST", self._url(path), data=data),
                                retry_transient=retry_transient,
                                retry_not_ready=retry_not_ready)

    # ------------------------------------------------------------- endpoints
    def me(self) -> dict:
        return self.get("me", fields="user_id,username,account_type,media_count")

    def recent_media(self, user_id: str, limit: int = 25) -> list[dict]:
        out = self.get(f"{user_id}/media", fields="id,caption,timestamp,media_type,permalink",
                       limit=limit)
        return out.get("data", [])

    def publishing_limit(self, user_id: str) -> dict:
        out = self.get(f"{user_id}/content_publishing_limit", fields="quota_usage,config")
        data = out.get("data") or [{}]
        return data[0]

    def create_container(self, user_id: str, **fields) -> dict:
        return self.post(f"{user_id}/media", **fields)

    def upload_video(self, container_id: str, path: str, uri: str | None = None) -> dict:
        """Resumable upload of a local video (officially a Facebook-Login feature;
        used here only as an opt-in / fallback)."""
        size = os.path.getsize(path)
        url = uri or f"{RUPLOAD_HOST}/{self.version}/{container_id}"
        headers = {"Authorization": f"OAuth {self.token}", "offset": "0",
                   "file_size": str(size)}
        with open(path, "rb") as fh:
            body = fh.read()
        return self._with_retry(lambda: self._call("POST", url, headers=headers, body=body,
                                                   timeout=600))

    def container_status(self, container_id: str) -> dict:
        return self.get(container_id, fields="status_code,status")

    def wait_until_ready(self, container_id: str) -> None:
        waited = 0.0
        while True:
            info = self.container_status(container_id)
            code = info.get("status_code")
            if code in ("FINISHED", "PUBLISHED"):
                return
            if code in ("ERROR", "EXPIRED"):
                raise InstagramError(f"Container {container_id} {code}: "
                                     f"{info.get('status', 'no details')}")
            if waited >= self.poll_timeout:
                raise InstagramError(f"Container {container_id} still {code} after "
                                     f"{int(waited)}s; giving up this run.")
            self.sleep(self.poll_interval)
            waited += self.poll_interval

    def publish(self, user_id: str, container_id: str) -> str:
        out = self.post(f"{user_id}/media_publish", creation_id=container_id,
                        retry_transient=False, retry_not_ready=True)
        return out["id"]

    def permalink(self, media_id: str) -> str | None:
        try:
            return self.get(media_id, fields="permalink").get("permalink")
        except InstagramError:
            return None

    def comment(self, media_id: str, message: str) -> str:
        return self.post(f"{media_id}/comments", message=message)["id"]

    def refresh_token(self) -> dict:
        """Exchange a valid long-lived token for a fresh 60-day one."""
        url = f"{GRAPH_HOST}/refresh_access_token"
        params = {"grant_type": "ig_refresh_token", "access_token": self.token}
        return self._with_retry(lambda: self._call("GET", url, params=params))
