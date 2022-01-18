from abc import ABC, abstractmethod
from typing import MutableMapping, Optional, Union, overload, Iterator, MutableSet
from io import BufferedIOBase, BytesIO

from typing_extensions import TypeGuard

from multiformats import CID, multicodec, multibase, multihash, varint
import dag_cbor
from dag_cbor.encoding import EncodableType as DagCborEncodable
from typing_validation import validate

import requests


ValueType = Union[bytes, DagCborEncodable]

RawCodec = multicodec.get("raw")
DagCborCodec = multicodec.get("dag-cbor")
DagPbCodec = multicodec.get("dag-pb")
Sha256Hash = multihash.get("sha2-256")


def is_cid_list(os: list[object]) -> TypeGuard[list[CID]]:
    return all(isinstance(o, CID) for o in os)


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

    def normalize_cid(self, cid: CID) -> CID: # pylint: disable=no-self-use
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

    def import_car(self, stream_or_bytes: Union[BufferedIOBase, bytes]) -> list[CID]:
        validate(stream_or_bytes, Union[BufferedIOBase, bytes])
        if isinstance(stream_or_bytes, bytes):
            stream: BufferedIOBase = BytesIO(stream_or_bytes)
        else:
            stream = stream_or_bytes

        roots = [self.normalize_cid(root) for root in decode_car_header(stream)]

        while (next_block := decode_raw_car_block(stream)) is not None:
            cid, data = next_block
            self.put_raw(bytes(data), cid.codec)

        return roots


def decode_car_header(stream: BufferedIOBase) -> list[CID]:
    """
    Decodes a CAR header and returns the list of contained roots.
    """
    header_size = varint.decode(stream)
    header = dag_cbor.decode(stream.read(header_size))
    if not isinstance(header, dict):
        raise ValueError("no valid CAR header found")
    roots = header["roots"]
    if not isinstance(roots, list):
        raise ValueError("CAR header doesn't contain roots")
    if not is_cid_list(roots):
        raise ValueError("CAR roots do not only contain CIDs")
    return roots


def decode_raw_car_block(stream: BufferedIOBase) -> Optional[tuple[CID, bytes]]:
    try:
        block_size = varint.decode(stream)
    except ValueError:
        # stream has likely been consumed entirely
        return None

    data = stream.read(block_size)
    # as the size of the CID is variable but not explicitly given in
    # the CAR format, we need to partially decode each CID to determine
    # its size and the location of the payload data
    if data[0] == 0x12 and data[1] == 0x20:
        # this is CIDv0
        cid_version = 0
        default_base = "base58btc"
        cid_codec: Union[int, multicodec.Multicodec] = DagPbCodec
        hash_codec: Union[int, multihash.Multihash] = Sha256Hash
        cid_digest = data[2:34]
        data = data[34:]
    else:
        # this is CIDv1(+)
        cid_version, _, data = varint.decode_raw(data)
        if cid_version != 1:
            raise ValueError(f"CIDv{cid_version} is currently not supported")
        default_base = "base32"
        cid_codec, _, data = multicodec.unwrap_raw(data)
        hash_codec, _, data = varint.decode_raw(data)
        digest_size, _, data = varint.decode_raw(data)
        cid_digest = data[:digest_size]
        data = data[digest_size:]
    cid = CID(default_base, cid_version, cid_codec, (hash_codec, cid_digest))

    if not cid.hashfun.digest(data) == cid.digest:
        raise ValueError(f"CAR is corrupted. Entry '{cid}' could not be verified")

    return cid, bytes(data)


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


class IPFSStore(ContentAddressableStore):
    def __init__(self,
                 host: str,
                 default_hash: Union[str, int, multicodec.Multicodec, multihash.Multihash] = "sha2-256",
                 ):
        validate(host, str)
        validate(default_hash, Union[str, int, multicodec.Multicodec, multihash.Multihash])

        self._host = host

        if isinstance(default_hash, multihash.Multihash):
            self._default_hash = default_hash
        else:
            self._default_hash = multihash.Multihash(codec=default_hash)

    def get_raw(self, cid: CID) -> bytes:
        validate(cid, CID)
        res = requests.post(self._host + "/api/v0/block/get", params={"arg": str(cid)})
        res.raise_for_status()
        return res.content

    def put_raw(self,
                raw_value: bytes,
                codec: Union[str, int, multicodec.Multicodec]) -> CID:
        validate(raw_value, bytes)
        validate(codec, Union[str, int, multicodec.Multicodec])

        if isinstance(codec, str):
            codec = multicodec.get(name=codec)
        elif isinstance(codec, int):
            codec = multicodec.get(code=codec)

        res = requests.post(self._host + "/api/v0/dag/put",
                            params={"store-codec": codec.name,
                                    "input-codec": codec.name,
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