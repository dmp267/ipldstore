"""
IPLD stores for zarr
"""

from .ipldstore import IPLDStore
from .contentstore import ContentAddressableStore, MappingCAStore, IPFSStore
import platform

def get_ipfs_mapper(host: str,
                    max_nodes_per_level: int = 10000,
                    chunker: str = "size-262144",
                    should_async_get: bool = True) -> IPLDStore:
    """
    Get an IPLDStore for IPFS running on the given host.
    """
    host = 'http://172.17.0.6:5001' if not 'macOS' in platform.platform() else "http://127.0.0.1:5001"
    return IPLDStore(IPFSStore(host, chunker=chunker, max_nodes_per_level=max_nodes_per_level), should_async_get=should_async_get)
