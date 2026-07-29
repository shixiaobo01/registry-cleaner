"""Small, defensive client for the Docker Distribution HTTP API V2."""
from __future__ import annotations

import json
import re
import threading
from typing import Dict, Iterator, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


MANIFEST_ACCEPT = ", ".join((
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
))


class RegistryError(RuntimeError):
    """An API response which makes it unsafe to continue normally."""


class RegistryClient:
    def __init__(self, base_url: str, username: str, password: str, *, verify=True,
                 timeout: int = 30, retries: int = 4, backoff: float = 1.0):
        self.base_url = base_url.rstrip("/") + "/"
        self.auth = (username, password)
        self.verify = verify
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self._local = threading.local()
        self._token_lock = threading.Lock()
        self._tokens: Dict[Tuple[str, str, str], str] = {}

    def _session(self) -> requests.Session:
        # requests.Session is not documented as thread-safe. Give each worker one.
        if not hasattr(self._local, "session"):
            retry = Retry(
                total=self.retries, connect=self.retries, read=self.retries,
                status=self.retries, backoff_factor=self.backoff,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(("GET", "HEAD", "DELETE")),
                respect_retry_after_header=True,
            )
            session = requests.Session()
            session.verify = self.verify
            session.headers.update({"User-Agent": "registry-cleaner/1.0"})
            adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._local.session = session
        return self._local.session

    @staticmethod
    def _bearer_challenge(response: requests.Response) -> Optional[Dict[str, str]]:
        """Parse the Registry's WWW-Authenticate: Bearer challenge."""
        header = response.headers.get("WWW-Authenticate", "")
        if not header.lower().startswith("bearer "):
            return None
        # Docker Distribution uses quoted key="value" parameters. This keeps
        # commas in an optional scope value intact because they are inside quotes.
        values = dict(re.findall(r'([A-Za-z]+)="([^"]*)"', header[7:]))
        return values if values.get("realm") else None

    def _token(self, challenge: Dict[str, str]) -> str:
        realm = challenge["realm"]
        service = challenge.get("service", "")
        scope = challenge.get("scope", "")
        key = (realm, service, scope)
        with self._token_lock:
            cached = self._tokens.get(key)
        if cached:
            return cached
        params = {key: value for key, value in (("service", service), ("scope", scope)) if value}
        try:
            response = self._session().get(realm, params=params, auth=self.auth, timeout=self.timeout)
        except requests.RequestException as exc:
            raise RegistryError("Token request %s failed: %s" % (realm, exc)) from exc
        if not response.ok:
            raise RegistryError("Token request %s returned HTTP %s: %s" % (realm, response.status_code, response.text[:500]))
        try:
            payload = response.json()
            token = payload.get("token") or payload.get("access_token")
        except ValueError as exc:
            raise RegistryError("Token response from %s is not JSON" % realm) from exc
        if not isinstance(token, str) or not token:
            raise RegistryError("Token response from %s contains no token" % realm)
        with self._token_lock:
            self._tokens[key] = token
        return token

    def _request(self, method: str, path_or_url: str, **kwargs) -> requests.Response:
        url = path_or_url if path_or_url.startswith(("http://", "https://")) else urljoin(self.base_url, path_or_url.lstrip("/"))
        try:
            # Try direct Basic Auth first. A registry protected by a token service
            # responds with a Bearer challenge, which we exchange below.
            response = self._session().request(method, url, timeout=self.timeout, auth=self.auth, **kwargs)
        except requests.RequestException as exc:
            raise RegistryError("%s %s failed: %s" % (method, url, exc)) from exc
        challenge = self._bearer_challenge(response) if response.status_code == 401 else None
        if challenge:
            token = self._token(challenge)
            headers = dict(kwargs.pop("headers", {}))
            headers["Authorization"] = "Bearer " + token
            try:
                response = self._session().request(method, url, timeout=self.timeout, headers=headers, **kwargs)
            except requests.RequestException as exc:
                raise RegistryError("%s %s with Bearer token failed: %s" % (method, url, exc)) from exc
        if not response.ok:
            detail = response.text[:500].replace("\n", " ")
            auth_type = response.headers.get("WWW-Authenticate", "")
            suffix = " (WWW-Authenticate: %s)" % auth_type if auth_type else ""
            raise RegistryError("%s %s returned HTTP %s: %s%s" % (method, url, response.status_code, detail, suffix))
        return response

    @staticmethod
    def _next_link(response: requests.Response) -> Optional[str]:
        # requests parses Link headers and handles quoted rel values.
        link = response.links.get("next", {}).get("url")
        return urljoin(response.url, link) if link else None

    def ping(self) -> None:
        self._request("GET", "/v2/")

    def _paged(self, path: str, field: str, page_size: int) -> Iterator[str]:
        next_url: Optional[str] = urljoin(self.base_url, path.lstrip("/"))
        params = {"n": page_size}
        while next_url:
            response = self._request("GET", next_url, params=params)
            params = None  # Link already contains the cursor and n.
            try:
                values = response.json().get(field, [])
            except (ValueError, AttributeError) as exc:
                raise RegistryError("Invalid JSON while paging %s" % path) from exc
            if not isinstance(values, list):
                raise RegistryError("Unexpected %r field while paging %s" % (field, path))
            for value in values:
                if isinstance(value, str):
                    yield value
            next_url = self._next_link(response)

    def repositories(self, page_size: int) -> Iterator[str]:
        return self._paged("/v2/_catalog", "repositories", page_size)

    def tags(self, repository: str, page_size: int) -> Iterator[str]:
        return self._paged("/v2/%s/tags/list" % repository, "tags", page_size)

    def manifest(self, repository: str, reference: str) -> Tuple[str, Dict, str]:
        """Return (digest, manifest JSON, content type) for a tag or digest."""
        response = self._request(
            "GET", "/v2/%s/manifests/%s" % (repository, reference),
            headers={"Accept": MANIFEST_ACCEPT},
        )
        digest = response.headers.get("Docker-Content-Digest")
        if not digest:
            raise RegistryError("Registry did not return Docker-Content-Digest for %s:%s" % (repository, reference))
        try:
            payload = response.json()
        except ValueError as exc:
            raise RegistryError("Manifest for %s:%s is not JSON" % (repository, reference)) from exc
        return digest, payload, response.headers.get("Content-Type", "")

    def blob_json(self, repository: str, digest: str) -> Tuple[Dict, str]:
        response = self._request("GET", "/v2/%s/blobs/%s" % (repository, digest))
        try:
            return response.json(), response.headers.get("Last-Modified", "")
        except ValueError as exc:
            raise RegistryError("Config blob %s in %s is not JSON" % (digest, repository)) from exc

    def delete_manifest(self, repository: str, digest: str) -> None:
        self._request("DELETE", "/v2/%s/manifests/%s" % (repository, digest))
