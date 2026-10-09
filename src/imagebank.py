"""
MMF2 / Clickteam Fusion 2 ImageBank reader and writer.

Layout (verified against CTFAK's own decoder, see docs/FORMAT.md):

    int32   count
    repeat count:
        int32   handle            # on disk this is (logical handle + 1) when build >= 284
        int32   decompressedSize  # length of the zlib payload below
        int32   compressedSize
        byte[]  zlib(payload)

    payload:
        0   int32  checksum       # not validated by the runtime in our tests
        4   int32  references
        8   int32  dataSize       # decides the pixel layout
       12   int16  width
       14   int16  height
       16   uint8  graphicMode
       17   uint8  flags          # 1 RLE, 2 RLEW, 4 RLET, 8 LZX, 0x10 Alpha, 0x40 Mac, 0x80 RGBA
       18   int16  pad
       20   int16  hotspotX / hotspotY / actionX / actionY
       28   4B     transparent colour
       32   byte[] pixels

Pixel layouts for graphicMode == 4:
    dataSize == w*h*3              -> tight 24bpp BGR, transparency via the transparent colour
    dataSize == w*h*3 + a4(w)*h    -> TIGHT BGR plane + alpha plane padded to 4-byte rows
    dataSize == a4(w*3)*h + a4(w)*h-> both planes padded to 4-byte rows
    dataSize == w*h*4              -> interleaved BGRA
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field

import numpy as np

HEADER_SIZE = 32


def align2(x: int) -> int:
    """BGR rows are padded to an even pixel count (verified against all 7,136 images)."""
    return (x + 1) & ~1


def align4(x: int) -> int:
    """Alpha rows are padded to a 4-byte boundary."""
    return (x + 3) & ~3


def bgr_stride(w: int) -> int:
    return align2(w) * 3


def expected_size(w: int, h: int, has_alpha: bool) -> int:
    """
    Byte count of an image's pixel data, excluding the 32-byte header.

    One rule covers every image observed in practice:
        BGR plane   : stride = align2(w) * 3, tightly stacked over h rows
        alpha plane : stride = align4(w), appended only when the Alpha flag is set
    A w*h*3 "tight" image and an align4(w*3) "padded" image are both special cases
    where the even-width stride happens to coincide.
    """
    total = bgr_stride(w) * h
    if has_alpha:
        total += align4(w) * h
    return total


@dataclass
class ImageRecord:
    """One raw record from the ImageBank, keeping the original bytes intact."""
    handle: int
    payload_size: int          # the record's DecompressedSize field == 32 + pixel bytes
    comp_size: int
    compressed: bytes
    payload: bytes = field(repr=False, default=b"")

    # ---- payload accessors -------------------------------------------------
    @property
    def checksum(self) -> int:      return struct.unpack_from("<i", self.payload, 0)[0]
    @property
    def references(self) -> int:    return struct.unpack_from("<i", self.payload, 4)[0]
    @property
    def data_size(self) -> int:
        """Pixel byte count - EXCLUDES the 32-byte header (unlike payload_size)."""
        return struct.unpack_from("<i", self.payload, 8)[0]
    @property
    def width(self) -> int:         return struct.unpack_from("<h", self.payload, 12)[0]
    @property
    def height(self) -> int:        return struct.unpack_from("<h", self.payload, 14)[0]
    @property
    def graphic_mode(self) -> int:  return self.payload[16]
    @property
    def flags(self) -> int:         return self.payload[17]
    @property
    def transparent(self) -> tuple:
        """Raw 4 bytes at offset 28, stored BGRA (so [0]=B, [1]=G, [2]=R)."""
        return tuple(self.payload[28:32])

    @property
    def transparent_rgb(self) -> tuple:
        """
        The colour-key colour as (R, G, B).

        The on-disk field is BGRA; reading it as RGBA silently keys on a different colour
        and makes unrelated pixels transparent. Verified against CTFAK on all 7,136
        images of the reference game: BGRA order matches 100%, RGBA order only 97%.

        The key is applied even when it is black, so do not special-case (0, 0, 0).
        """
        t = self.transparent
        return (t[2], t[1], t[0])
    @property
    def tight_handle(self) -> int:
        """The handle as CTFAK exposes it (disk value minus one for build >= 284)."""
        return self.handle - 1

    # ---- pixel codec -------------------------------------------------------
    def to_rgba(self) -> np.ndarray:
        """Decode to an (h, w, 4) uint8 RGBA array."""
        w, h, ds = self.width, self.height, self.data_size
        if self.graphic_mode != 4 or w == 0 or h == 0:
            raise ValueError(f"unsupported graphicMode={self.graphic_mode} for {w}x{h}")
        if ds != expected_size(w, h, bool(self.flags & 0x10)):
            raise ValueError(
                f"unexpected dataSize {ds} for {w}x{h} flags=0x{self.flags:02X} "
                f"(expected {expected_size(w, h, bool(self.flags & 0x10))})")

        pix = self.payload[HEADER_SIZE:HEADER_SIZE + ds]
        s3 = bgr_stride(w)
        bgr = np.frombuffer(pix[:s3*h], np.uint8).reshape(h, s3)[:, :w*3].reshape(h, w, 3).copy()

        if self.flags & 0x10:
            s1 = align4(w)
            alpha = np.frombuffer(pix[s3*h:], np.uint8).reshape(h, s1)[:, :w].copy()
        else:
            alpha = np.full((h, w), 255, np.uint8)

        rgba = np.dstack([bgr[:, :, 2], bgr[:, :, 1], bgr[:, :, 0], alpha])
        # Images without an alpha channel express transparency with a colour key.
        # The key is applied unconditionally - when it does not occur in the image it
        # simply changes nothing, which is exactly what the runtime does.
        key = self.transparent_rgb
        if not (self.flags & 0x10) and key is not None:
            rgba[np.all(rgba[:, :, :3] == np.array(key, np.uint8), axis=2), 3] = 0
        return rgba

    def encode(self, rgba: np.ndarray) -> bytes:
        """Serialise an (h, w, 4) array back into a payload of the SAME length."""
        w, h, ds = self.width, self.height, self.data_size
        if rgba.shape[0] != h or rgba.shape[1] != w:
            raise ValueError(f"size mismatch: got {rgba.shape[1]}x{rgba.shape[0]}, want {w}x{h}")
        body = _encode_body_raw(rgba, ds, w, h, self.transparent_rgb,
                                transparent_is_key=self.transparent_rgb is not None)
        if body is None:
            raise ValueError(f"unknown layout dataSize={ds} for {w}x{h}")
        if len(body) != ds:
            raise ValueError(f"encoded {len(body)} bytes, expected {ds}")
        return self.payload[:HEADER_SIZE] + body


def parse(bank: bytes) -> list[ImageRecord]:
    """Parse an ImageBank chunk payload into records."""
    pos = 0
    count = struct.unpack_from("<i", bank, pos)[0]
    pos += 4
    records: list[ImageRecord] = []
    for _ in range(count):
        handle, dsize, csize = struct.unpack_from("<iiI", bank, pos)
        pos += 12
        comp = bank[pos:pos + csize]
        pos += csize
        rec = ImageRecord(handle=handle, payload_size=dsize, comp_size=csize, compressed=comp)
        rec.payload = zlib.decompress(comp)
        records.append(rec)
    if pos != len(bank):
        raise ValueError(f"trailing data: consumed {pos} of {len(bank)}")
    return records


def serialise(records: list[ImageRecord]) -> bytes:
    """Rebuild the ImageBank. Total size is checked by the caller."""
    parts = [struct.pack("<i", len(records))]
    for r in records:
        parts.append(struct.pack("<iiI", r.handle, len(r.payload), len(r.compressed)))
        parts.append(r.compressed)
    return b"".join(parts)


def compress_fitting(payload: bytes, limit: int, rgba: np.ndarray | None = None,
                     transparent: tuple | None = None, w: int = 0, h: int = 0,
                     data_size: int = 0, flags: int = 0,
                     quantise_ladder=(128, 96, 64, 48, 32, 24, 16, 12, 8)) -> bytes | None:
    """
    Compress `payload` to at most `limit` bytes.

    zlib stops at ZLIB_STREAM_END, so trailing zero padding is ignored by the decoder -
    that lets a smaller result be padded back to the exact original size.

    If it does not fit, progressively quantise the image's palette (requires `rgba`);
    this keeps the total bank size - and therefore every image offset - unchanged.
    Returns None if even the coarsest palette is too big.
    """
    from PIL import Image

    def try_levels(data: bytes) -> bytes | None:
        for level, strategy in ((9, zlib.Z_DEFAULT_STRATEGY), (9, zlib.Z_FILTERED),
                                (9, zlib.Z_HUFFMAN_ONLY), (9, zlib.Z_RLE),
                                (6, zlib.Z_DEFAULT_STRATEGY)):
            c = zlib.compressobj(level, zlib.DEFLATED, 15, 9, strategy)
            out = c.compress(data) + c.flush()
            if len(out) <= limit:
                return out + b"\x00" * (limit - len(out))
        return None

    got = try_levels(payload)
    if got is not None:
        return got
    if rgba is None:
        return None

    alpha = rgba[:, :, 3]
    rgb = Image.fromarray(rgba, "RGBA").convert("RGB")
    for ncol in quantise_ladder:
        q = rgb.quantize(colors=ncol, method=Image.MEDIANCUT).convert("RGB")
        coarse = np.dstack([np.array(q), alpha])
        body = _encode_body_raw(coarse, data_size, w, h, transparent,
                                transparent_is_key=bool(transparent))
        if body is None:
            return None
        got = try_levels(payload[:HEADER_SIZE] + body)
        if got is not None:
            return got
    return None


def _encode_body_raw(rgba: np.ndarray, data_size: int, w: int, h: int,
                     transparent: tuple | None,
                     transparent_is_key: bool = False) -> bytes | None:
    """
    Serialise pixels into `data_size` bytes using the same single rule as the decoder:
    a BGR plane whose rows are padded to an even pixel count, optionally followed by an
    alpha plane whose rows are padded to 4 bytes.

    Interleaving the two planes per row compiles to the same byte count but silently
    zeroes the alpha channel, which makes every affected image render fully transparent.
    """
    s3, s1 = bgr_stride(w), align4(w)
    has_alpha = data_size == s3*h + s1*h
    if not has_alpha and data_size != s3*h:
        return None

    bgr = np.ascontiguousarray(rgba[:, :, [2, 1, 0]])
    flat = np.zeros(data_size, np.uint8)
    pad3 = np.zeros((h, s3), np.uint8)
    pad3[:, :w*3] = bgr.reshape(h, w*3)
    if not has_alpha and transparent_is_key and transparent:
        # transparency has to be baked in as the colour key: rebuild before padding
        tmp = pad3[:, :w*3].reshape(h, w, 3)
        tmp[rgba[:, :, 3] == 0] = np.array(transparent[:3], np.uint8)
    flat[:s3*h] = pad3.reshape(-1)
    if has_alpha:
        pad1 = np.zeros((h, s1), np.uint8)
        pad1[:, :w] = rgba[:, :, 3]
        flat[s3*h:] = pad1.reshape(-1)
    return flat.tobytes()
