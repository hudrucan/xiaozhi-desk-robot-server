"""Legacy runtime adapter; deterministic memory logic is provider independent."""
from core.explicit_memory import ExplicitMemory
from ..base import MemoryProviderBase, logger


class MemoryProvider(ExplicitMemory, MemoryProviderBase):
    def __init__(self, config, summary_memory=None):
        super().__init__(config, summary_memory, logger=logger.bind(tag=__name__))
