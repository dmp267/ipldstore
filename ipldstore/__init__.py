"""
IPLD stores for zarr
"""

from .ipldstore import IPLDStore
from .contentstore import ContentAddressableStore, MappingCAStore, IPFSStore

import os, json


ADAPTER_SECRETS = os.getenv("ADAPTER_SECRETS", None)
IPFS_HOST = os.getenv("IPFS_HOST", None)
if ADAPTER_SECRETS is not None:
    IPFS_HOST = f'http://{json.loads(ADAPTER_SECRETS).get("IPFS_HOST", None)}'
elif IPFS_HOST is not None:
    IPFS_HOST = f'http://{IPFS_HOST}'
print(f'IPFS HOST base: {IPFS_HOST}')


def get_ipfs_mapper(
    host: str = "http://0.0.0.0:5001",
    max_nodes_per_level: int = 10000,
    chunker: str = "size-262144",
    should_async_get: bool = True,
) -> IPLDStore:
    """
    Get an IPLDStore for IPFS running on the given host.
    """
    if IPFS_HOST is not None:
        host = IPFS_HOST
    print(f'ipfs mapper host: {host}')

    return IPLDStore(IPFSStore(host, chunker=chunker, max_nodes_per_level=max_nodes_per_level), should_async_get=should_async_get)