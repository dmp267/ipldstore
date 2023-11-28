from abc import ABC, abstractmethod
from typing import Dict, MutableMapping, Optional, Union, overload, Iterator, MutableSet, List
from io import BufferedIOBase, BytesIO
from itertools import zip_longest

import aiohttp
import asyncio

from multiformats import CID, multicodec, multibase, multihash, varint
import cbor2, dag_cbor
from cbor2 import CBORTag
from dag_cbor.encoding import EncodableType as DagCborEncodable
from typing_validation import validate

import requests
from requests.adapters import HTTPAdapter, Retry

from .car import read_car
from .utils import StreamLike


ValueType = Union[bytes, DagCborEncodable]

RawCodec = multicodec.get("raw")
DagPbCodec = multicodec.get("dag-pb")
DagCborCodec = multicodec.get("dag-cbor")

def default_encoder(encoder, value):
    encoder.encode(CBORTag(42,  b'\x00' + bytes(value)))

def grouper(seq, size):
    return (seq[pos:pos + size] for pos in range(0, len(seq), size))


def get_retry_session() -> requests.Session:
    session =  requests.Session()
    retries = Retry(connect=5, total=5, backoff_factor=4)
    session.mount("http://", HTTPAdapter(max_retries=retries))
    return session


class ContentAddressableStore(ABC):
    @abstractmethod
    def get_raw(self, cid: CID) -> bytes:
        ...

    def get(self, cid: CID) -> ValueType:
        value = self.get_raw(cid)
        if cid.codec == RawCodec:
            return value
        elif cid.codec == DagCborCodec:
            return dag_cbor.decode(value)
        else:
            raise ValueError(f"can't decode CID's codec '{cid.codec.name}'")

    def __contains__(self, cid: CID) -> bool:
        try:
            self.get_raw(cid)
        except KeyError:
            return False
        else:
            return True

    @abstractmethod
    def put_raw(self,
                raw_value: bytes,
                codec: Union[str, int, multicodec.Multicodec]) -> CID:
        ...

    def put(self, value: ValueType) -> CID:
        validate(value, ValueType)
        if isinstance(value, bytes):
            return self.put_raw(value, RawCodec)
        else:
            return self.put_raw(dag_cbor.encode(value), DagCborCodec)

    def normalize_cid(self, cid: CID) -> CID:  # pylint: disable=no-self-use
        return cid

    @overload
    def to_car(self, root: CID, stream: BufferedIOBase) -> int:
        ...

    @overload
    def to_car(self, root: CID, stream: None = None) -> bytes:
        ...

    def to_car(self, root: CID, stream: Optional[BufferedIOBase] = None) -> Union[bytes, int]:
        validate(root, CID)
        validate(stream, Optional[BufferedIOBase])

        if stream is None:
            buffer = BytesIO()
            stream = buffer
            return_bytes = True
        else:
            return_bytes = False

        bytes_written = 0
        header = dag_cbor.encode({"version": 1, "roots": [root]})
        bytes_written += stream.write(varint.encode(len(header)))
        bytes_written += stream.write(header)
        bytes_written += self._to_car(root, stream, set())

        if return_bytes:
            return buffer.getvalue()
        else:
            return bytes_written

    def _to_car(self,
                root: CID,
                stream: BufferedIOBase,
                already_written: MutableSet[CID]) -> int:
        """
            makes a CAR without the header
        """
        bytes_written = 0

        if root not in already_written:
            data = self.get_raw(root)
            cid_bytes = bytes(root)
            bytes_written += stream.write(varint.encode(len(cid_bytes) + len(data)))
            bytes_written += stream.write(cid_bytes)
            bytes_written += stream.write(data)
            already_written.add(root)

            if root.codec == DagCborCodec:
                value = dag_cbor.decode(data)
                for child in iter_links(value):
                    bytes_written += self._to_car(child, stream, already_written)
        return bytes_written

    def import_car(self, stream_or_bytes: StreamLike) -> List[CID]:
        roots, blocks = read_car(stream_or_bytes)
        roots = [self.normalize_cid(root) for root in roots]

        for cid, data, _ in blocks:
            self.put_raw(bytes(data), cid.codec)

        return roots


class MappingCAStore(ContentAddressableStore):
    def __init__(self,
                 mapping: Optional[MutableMapping[str, bytes]] = None,
                 default_hash: Union[str, int, multicodec.Multicodec, multihash.Multihash] = "sha2-256",
                 default_base: Union[str, multibase.Multibase] = "base32",
                 ):
        validate(mapping, Optional[MutableMapping[str, bytes]])
        validate(default_hash, Union[str, int, multicodec.Multicodec, multihash.Multihash])
        validate(default_base, Union[str, multibase.Multibase])

        if mapping is not None:
            self._mapping = mapping
        else:
            self._mapping = {}

        if isinstance(default_hash, multihash.Multihash):
            self._default_hash = default_hash
        else:
            self._default_hash = multihash.Multihash(codec=default_hash)

        if isinstance(default_base, multibase.Multibase):
            self._default_base = default_base
        else:
            self._default_base = multibase.get(default_base)

    def normalize_cid(self, cid: CID) -> CID:
        return cid.set(base=self._default_base, version=1)

    def get_raw(self, cid: CID) -> bytes:
        validate(cid, CID)
        return self._mapping[str(self.normalize_cid(cid))]

    def put_raw(self,
                raw_value: bytes,
                codec: Union[str, int, multicodec.Multicodec]) -> CID:
        validate(raw_value, bytes)
        validate(codec, Union[str, int, multicodec.Multicodec])

        h = self._default_hash.digest(raw_value)
        cid = CID(self._default_base, 1, codec, h)
        self._mapping[str(cid)] = raw_value
        return cid


async def _async_get(host: str, session: aiohttp.ClientSession, cid: CID):
    if cid.codec == DagPbCodec:
        api_method = "/api/v0/cat"
    else:
        api_method = "/api/v0/block/get"
    async with session.post(host + api_method, params={"arg": str(cid)}) as resp:
        return await resp.read()

async def _main_async(keys: List[CID], host: str, d: Dict[CID, bytes]):
    async with aiohttp.ClientSession() as session:
        tasks = [_async_get(host, session, key) for key in keys]
        byte_list = await asyncio.gather(*tasks)
        for i, key in enumerate(keys):
            d[key] = byte_list[i]


class IPFSStore(ContentAddressableStore):
    def __init__(self,
                 host: str,
                 chunker: str = "size-262144",
                 max_nodes_per_level: int = 10000,
                 default_hash: Union[str, int, multicodec.Multicodec, multihash.Multihash] = "sha2-256",
                 ):
        validate(host, str)
        validate(default_hash, Union[str, int, multicodec.Multicodec, multihash.Multihash])

        self._host = host
        self._chunker = chunker
        self._max_nodes_per_level = max_nodes_per_level

        if isinstance(default_hash, multihash.Multihash):
            self._default_hash = default_hash
        else:
            self._default_hash = multihash.Multihash(codec=default_hash)

    def recover_tree(self, broken_struct):
        if not isinstance(broken_struct, dict):
            return broken_struct
        all_recovered = []
        ret_tree = {}
        for k in broken_struct:
            if len(k) > 1 and k.startswith("/") and k[2:].isnumeric():
                cid_to_recover = CID.decode(broken_struct[k].value[1:])
                recovered = self.recover_tree(cbor2.loads(self.get_raw(cid_to_recover)))
                all_recovered.append(recovered)
            else:
                ret_tree[k] = self.recover_tree(broken_struct[k])
        for recovered in all_recovered:
            for k in recovered:
                ret_tree[k] = self.recover_tree(recovered[k])

        return ret_tree

    def get(self, cid: CID) -> ValueType:
        print(f'CID {cid} of raw')
        value = self.get_raw(cid)
        print(f'raw {value}')
        if cid.codec == DagPbCodec:
            return value
        elif cid.codec == DagCborCodec:
            return self.recover_tree(cbor2.loads(value))
        else:
            raise ValueError(f"can't decode CID's codec '{cid.codec.name}'")

    def getitems(self, keys: List[CID]) -> Dict[CID, bytes]:
        ret = {}
        asyncio.run(_main_async(keys, self._host, ret))
        return ret

    def get_raw(self, cid: CID) -> bytes:
        print(f'validating')
        validate(cid, CID)

        session = get_retry_session()
    
        if cid.codec == DagPbCodec:
            print(f'catting {str(cid)} with host {self._host}')
            res = session.post(self._host + "/api/v0/cat", params={"arg": str(cid)})
        else:
            print(f'getting block {str(cid)} with host {self._host}')
            res = session.post(self._host + "/api/v0/block/get", params={"arg": str(cid)})
        res.raise_for_status()
        return res.content

    def make_tree_structure(self, node):
        if not isinstance(node, dict):
            return node
        new_tree = {}
        if len(node) <= self._max_nodes_per_level:
            for key in node:
                new_tree[key] = self.make_tree_structure(node[key])
            return new_tree
        for group_of_keys in grouper(list(node.keys()), self._max_nodes_per_level):
            key_for_group = f"/{hash(frozenset(group_of_keys))}"
            sub_tree = {}
            for key in group_of_keys:
                sub_tree[key] = node[key]
            new_tree[key_for_group] = self.put_sub_tree(self.make_tree_structure(sub_tree))
        return self.make_tree_structure(new_tree)

    def put_sub_tree(self, d):
        return self.put_raw(cbor2.dumps(d, default=default_encoder), DagCborCodec, should_pin=False)

    def put(self, value: ValueType) -> CID:
        validate(value, ValueType)
        if isinstance(value, bytes):
            return self.put_raw(value, DagPbCodec)
        else:
            return self.put_raw(cbor2.dumps(self.make_tree_structure(value), default=default_encoder), DagCborCodec)

    def put_raw(self,
                raw_value: bytes,
                codec: Union[str, int, multicodec.Multicodec],
                should_pin=True) -> CID:
        validate(raw_value, bytes)
        validate(codec, Union[str, int, multicodec.Multicodec])

        if isinstance(codec, str):
            codec = multicodec.get(name=codec)
        elif isinstance(codec, int):
            codec = multicodec.get(code=codec)

        session = get_retry_session()

        if codec == DagPbCodec:
            res = session.post(self._host + "/api/v0/add",
                                params={"pin": False, "chunker": self._chunker},
                                files={"dummy": raw_value})
            res.raise_for_status()
            return CID.decode(res.json()["Hash"])
        else:
            res = session.post(self._host + "/api/v0/dag/put",
                            params={"store-codec": codec.name,
                                    "input-codec": codec.name,
                                    "pin": should_pin,
                                    "hash": self._default_hash.name},
                            files={"dummy": raw_value})
            res.raise_for_status()
            return CID.decode(res.json()["Cid"]["/"])


def iter_links(o: DagCborEncodable) -> Iterator[CID]:
    if isinstance(o, dict):
        for v in o.values():
            yield from iter_links(v)
    elif isinstance(o, list):
        for v in o:
            yield from iter_links(v)
    elif isinstance(o, CID):
        yield o


__all__ = ["ContentAddressableStore", "MappingCAStore", "iter_links"]
