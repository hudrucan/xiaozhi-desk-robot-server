"""Small Drive blob transport; storage logic does not depend on HTTP or OAuth."""

import json
import re
import uuid
from pathlib import Path
from typing import Protocol

from config.config_loader import get_project_dir
from config.config_store import ConfigConflict, ConfigUnavailable


class DriveTransport(Protocol):
    def list_state_descriptors(self) -> list[dict]: ...
    def create_state_descriptor(self, folder_id: str, content: bytes, source_id: str) -> str: ...
    def validate_folder(self, folder_id: str) -> None: ...
    def create_folder(self, name: str) -> str: ...
    def read_manifest(self, file_id: str) -> tuple[bytes, str]: ...
    def download(self, file_id: str) -> bytes: ...
    def upload_immutable(self, folder_id: str, content: bytes, name: str) -> str: ...
    def upload_blob(self, folder_id: str, content: bytes, name: str, mime_type: str) -> str: ...
    def replace_manifest(self, file_id: str, content: bytes, etag: str) -> None: ...


class GoogleDriveTransport:
    """Use v2's explicit file ETag for conditional manifest publication.

    Credentials are loaded lazily, so missing credentials/dependencies still
    permit offline boot from LKG. No credential bytes enter the config store.
    """

    API = "https://www.googleapis.com/drive/v2/files"
    UPLOAD = "https://www.googleapis.com/upload/drive/v2/files"
    SCOPES = ["https://www.googleapis.com/auth/drive.file"]

    def __init__(self, credentials_path, session=None):
        self.credentials_path = Path(credentials_path)
        if not self.credentials_path.is_absolute():
            self.credentials_path = Path(get_project_dir()) / self.credentials_path
        self.session = session

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", value):
            raise ValueError("Invalid Drive file/folder identity")
        return value

    def _request(self, method, url, **kwargs):
        try:
            if self.session is None:
                import google.auth
                from google.auth.transport.requests import AuthorizedSession
                credentials, _ = google.auth.load_credentials_from_file(
                    str(self.credentials_path), scopes=self.SCOPES
                )
                self.session = AuthorizedSession(credentials, refresh_timeout=20)
            response = self.session.request(method, url, timeout=20, **kwargs)
        except Exception as error:
            # Transport/auth exceptions may contain credentials or response bodies.
            raise ConfigUnavailable("Drive request unavailable") from error
        if response.status_code == 412:
            raise ConfigConflict("Cloud manifest changed during save; reload before retrying")
        if not 200 <= response.status_code < 300:
            raise ConfigUnavailable(f"Drive request failed (HTTP {response.status_code})")
        return response

    def _metadata(self, file_id):
        value = self._request("GET", f"{self.API}/{self._id(file_id)}", params={
            "fields": "id,etag", "supportsAllDrives": "true",
        }).json()
        if not isinstance(value, dict):
            raise ConfigUnavailable("Invalid Drive file metadata")
        return value

    def download(self, file_id):
        return self._request("GET", f"{self.API}/{self._id(file_id)}", params={
            "alt": "media", "supportsAllDrives": "true",
        }).content

    def download_limited(self, file_id, max_bytes):
        """Bound background Soundbank downloads independently of response headers."""
        with self._request("GET", f"{self.API}/{self._id(file_id)}", params={
            "alt": "media", "supportsAllDrives": "true",
        }, stream=True) as response:
            content = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if len(content) + len(chunk) > max_bytes:
                    raise ConfigUnavailable("Cloud asset exceeds the local sync limit")
                content.extend(chunk)
            return bytes(content)

    def read_manifest(self, file_id):
        before = self._metadata(file_id).get("etag")
        content = self.download(file_id)
        after = self._metadata(file_id).get("etag")
        if not isinstance(before, str) or not before or before != after:
            raise ConfigConflict("Cloud manifest changed during read; retry sync")
        return content, before

    def create_folder(self, name):
        """Create an app-owned private My Drive folder with drive.file scope."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Drive folder name must be non-empty")
        response = self._request("POST", self.API, params={
            "fields": "id", "supportsAllDrives": "true",
        }, json={"title": name, "mimeType": "application/vnd.google-apps.folder"})
        value = response.json()
        if not isinstance(value, dict):
            raise ConfigUnavailable("Invalid Drive folder metadata")
        return self._id(value.get("id"))

    def validate_folder(self, folder_id):
        """Read-only provisioning preflight with the existing drive.file scope."""
        value = self._request("GET", f"{self.API}/{self._id(folder_id)}", params={
            "fields": "id,mimeType,capabilities(canAddChildren)", "supportsAllDrives": "true",
        }).json()
        if (not isinstance(value, dict) or value.get("id") != folder_id
                or value.get("mimeType") != "application/vnd.google-apps.folder"
                or value.get("capabilities", {}).get("canAddChildren") is not True):
            raise ConfigUnavailable("Drive folder is unavailable for provisioning")

    def upload_immutable(self, folder_id, content, name):
        return self._upload(folder_id, content, name, "application/json")

    def upload_blob(self, folder_id, content, name, mime_type):
        if mime_type not in {"audio/wav", "audio/mpeg", "application/octet-stream"}:
            raise ValueError("Unsupported soundbank blob MIME type")
        return self._upload(folder_id, content, name, mime_type)

    def list_state_descriptors(self):
        """Discover private app metadata, never arbitrary filenames or broader scope."""
        result, seen = [], set()
        params = {"q": "trashed = false and properties has { key='xiaozhi_cloud_state' and value='v1' and visibility='PRIVATE' }",
                  "fields": "items(id,properties),nextPageToken,incompleteSearch", "maxResults": 100,
                  "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        while True:
            page = self._request("GET", self.API, params=params).json()
            if not isinstance(page, dict) or page.get("incompleteSearch") or not isinstance(page.get("items"), list):
                raise ConfigUnavailable("Cloud State discovery unavailable")
            for item in page["items"]:
                properties = {prop["key"]: prop.get("value") for prop in item.get("properties", [])
                              if prop.get("visibility") == "PRIVATE"}
                if properties.get("xiaozhi_cloud_state") != "v1":
                    raise ConfigUnavailable("Invalid Cloud State discovery metadata")
                result.append({"file_id": self._id(item.get("id")), "source_id": properties.get("source_id")})
            token = page.get("nextPageToken")
            if not token:
                return result
            if not isinstance(token, str) or token in seen:
                raise ConfigUnavailable("Cloud State discovery unavailable")
            seen.add(token)
            params = {**params, "pageToken": token}

    def create_state_descriptor(self, folder_id, content, source_id):
        return self._upload(folder_id, content, "cloud-state.json", "application/json", properties=[
            {"key": "xiaozhi_cloud_state", "value": "v1", "visibility": "PRIVATE"},
            {"key": "source_id", "value": str(uuid.UUID(source_id)), "visibility": "PRIVATE"},
        ])

    def _upload(self, folder_id, content, name, mime_type, *, properties=None):
        boundary = "config_" + uuid.uuid4().hex
        metadata_value = {
            "title": name, "mimeType": mime_type,
            "parents": [{"id": self._id(folder_id)}],
        }
        if properties is not None:
            metadata_value["properties"] = properties
        metadata = json.dumps(metadata_value).encode()
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
                + metadata + f"\r\n--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n".encode()
                + content + f"\r\n--{boundary}--\r\n".encode())
        response = self._request("POST", self.UPLOAD, params={
            "uploadType": "multipart", "fields": "id", "supportsAllDrives": "true",
        }, headers={"Content-Type": f"multipart/related; boundary={boundary}"}, data=body)
        value = response.json()
        if not isinstance(value, dict):
            raise ConfigUnavailable("Invalid Drive upload metadata")
        return self._id(value.get("id"))

    def replace_manifest(self, file_id, content, etag):
        if not isinstance(etag, str) or not etag or etag == "*":
            raise ConfigUnavailable("Drive manifest requires an ETag for conditional commit")
        self._request("PUT", f"{self.UPLOAD}/{self._id(file_id)}", params={
            "uploadType": "media", "supportsAllDrives": "true",
        }, headers={"Content-Type": "application/json", "If-Match": etag}, data=content)
