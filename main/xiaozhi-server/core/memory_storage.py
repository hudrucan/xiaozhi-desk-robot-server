"""Safe storage domain errors shared by memory providers, HTTP and tools."""


class MemoryStorageError(Exception):
    message = "Memory storage unavailable"

    def __init__(self):
        super().__init__(self.message)


class MemoryUnavailable(MemoryStorageError):
    message = "Cloud Memory unavailable; writes require an online connection"


class MemoryConflict(MemoryStorageError):
    message = "Cloud Memory changed; sync and review before retrying"


class MemoryReadOnly(MemoryStorageError):
    message = "Cloud Memory is read-only on this server node"


class MemoryReconciliationRequired(MemoryUnavailable):
    message = "Cloud Memory requires provisioning/reconciliation before switching sources"
