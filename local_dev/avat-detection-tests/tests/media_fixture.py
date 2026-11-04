"""Small AVI containing MJPEG frames and an audible PCM tone; no external tools."""

import struct
from pathlib import Path

import cv2
import numpy as np


def create_av_clip(path: Path, *, with_audio: bool = True) -> Path:
    # RIFF stream/chunk layout follows Microsoft's AVI RIFF File Reference.
    def chunk(tag: bytes, content: bytes) -> bytes:
        return tag + struct.pack("<I", len(content)) + content + b"\0" * (len(content) % 2)

    def listing(tag: bytes, content: bytes) -> bytes:
        return chunk(b"LIST", tag + content)

    fps, frames, width, height, sample_rate = 20, 60, 64, 48, 8000
    ok, encoded = cv2.imencode(".jpg", np.full((height, width, 3), 100, dtype=np.uint8))
    assert ok
    jpeg = encoded.tobytes()
    main = struct.pack("<14I", 1_000_000 // fps, 50_000, 0, 0x110, frames, 0,
                       2 if with_audio else 1, len(jpeg), width, height, 0, 0, 0, 0)
    video_header = struct.pack("<4s4sIHH8I4h", b"vids", b"MJPG", 0, 0, 0,
                               0, 1, fps, 0, frames, len(jpeg), 0xFFFFFFFF, 0,
                               0, 0, width, height)
    video_format = struct.pack("<IiiHH4sIiiII", 40, width, height, 1, 24,
                               b"MJPG", width * height * 3, 0, 0, 0, 0)
    headers = chunk(b"avih", main) + listing(
        b"strl", chunk(b"strh", video_header) + chunk(b"strf", video_format)
    )
    samples = (8000 * np.sin(2 * np.pi * 440 * np.arange(sample_rate * 3) / sample_rate)).astype("<i2")
    if with_audio:
        audio_header = struct.pack("<4s4sIHH8I4h", b"auds", b"\0" * 4, 0, 0, 0,
                                   0, 2, sample_rate * 2, 0, sample_rate * 3,
                                   sample_rate * 2 // fps, 0xFFFFFFFF, 2, 0, 0, 0, 0)
        audio_format = struct.pack("<HHIIHHH", 1, 1, sample_rate, sample_rate * 2, 2, 16, 0)
        headers += listing(b"strl", chunk(b"strh", audio_header) + chunk(b"strf", audio_format))
    movie, index = bytearray(), bytearray()
    for frame in range(frames):
        pieces = [(b"00dc", jpeg, 0x10)]
        if with_audio:
            start = frame * sample_rate // fps
            pieces.append((b"01wb", samples[start:start + sample_rate // fps].tobytes(), 0))
        for tag, content, flags in pieces:
            index.extend(struct.pack("<4sIII", tag, flags, len(movie) + 4, len(content)))
            movie.extend(chunk(tag, content))
    content = b"AVI " + listing(b"hdrl", headers) + listing(b"movi", movie) + chunk(b"idx1", index)
    path.write_bytes(chunk(b"RIFF", content))
    return path
