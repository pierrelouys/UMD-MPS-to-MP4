#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
UMD Video MPS -> 264/OMA -> MP4 converter for Windows 10.

Input:
    A folder containing one or more .MPS files.

For each MPS this script:
    1. Demultiplexes the Sony PSP UMD MPS MPEG-PS stream into:
         <name>_000001E0.264
         <name>_000001BD.oma       (first ATRAC3 stream, with PSP EA3/AA3 header)
         <name>_<streamid>0001BD.oma for additional ATRAC3 streams
    2. Finalizes the extracted ATRAC payload as a PSP OMA (EA3/AA3 container).
    3. Runs:
         ffmpeg.exe -y -hide_banner
             -i VIDEO.264
             -i AUDIO.oma
             -map 0:v:0 -map 1:a:0
             -c:v copy
             -c:a aac -b:a 192k
             OUTPUT.mp4

The video is NOT re-encoded. Audio is transcoded to AAC 192k as requested.

Usage from CMD:
    python umd_mps_to_mp4.py "C:\\path\\to\\folder"

If no folder is supplied, the script uses its own folder.

No pywinauto, GUI automation, or VGMToolbox executable is required.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple


PACK_START = b"\x00\x00\x01\xBA"
PROGRAM_END = b"\x00\x00\x01\xB9"

# MpegStream.cs recognizes these variable-length packet IDs.
VARIABLE_CODES = (
    {0xBB, 0xBD, 0xBE, 0xBF}
    | set(range(0xC0, 0xE0))
    | set(range(0xE0, 0xF0))
)

CHUNK_SIZE = 1024 * 1024


class DemuxError(RuntimeError):
    pass


def find_ffmpeg() -> str:
    """Find ffmpeg.exe beside this script or on PATH."""
    candidates = [
        Path(__file__).resolve().parent / "ffmpeg.exe",
        Path.cwd() / "ffmpeg.exe",
    ]

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    found = shutil.which("ffmpeg.exe") or shutil.which("ffmpeg")
    if found:
        return found

    raise FileNotFoundError(
        "ffmpeg.exe was not found. Put ffmpeg.exe on PATH or beside this script."
    )


def find_first_pack_header(stream) -> int:
    """
    VGMToolbox calls GetNextOffset(..., 00 00 01 BA).
    Do the same without loading the whole MPS into memory.
    """
    base = stream.tell()
    overlap = b""

    while True:
        chunk = stream.read(CHUNK_SIZE)
        if not chunk:
            return -1

        data = overlap + chunk
        idx = data.find(PACK_START)
        if idx >= 0:
            return base + idx - len(overlap)

        overlap = data[-3:]
        base += len(chunk)


def copy_range(src, dst, start: int, length: int) -> None:
    """Copy exactly length bytes from an open input stream to an output stream."""
    if length <= 0:
        return

    src.seek(start)
    remaining = length

    while remaining:
        block = src.read(min(CHUNK_SIZE, remaining))
        if not block:
            raise DemuxError(
                f"Unexpected EOF while extracting {length} bytes at 0x{start:X}"
            )
        dst.write(block)
        remaining -= len(block)


def remove_all_chunks(path: Path, chunk: bytes) -> None:
    """
    Port of VGMToolbox FileUtil.RemoveAllChunksFromFile().
    It removes every exact occurrence of the supplied 8-byte header chunk.
    """
    if len(chunk) == 0:
        return

    temp = path.with_suffix(path.suffix + ".cut")
    tail = b""

    with path.open("rb") as src, temp.open("wb") as dst:
        while True:
            data = src.read(CHUNK_SIZE)
            if not data:
                break

            buf = tail + data

            if len(buf) > len(chunk) - 1:
                split_at = len(buf) - (len(chunk) - 1)
                emit = buf[:split_at]
                tail = buf[split_at:]
                if emit:
                    dst.write(emit.replace(chunk, b""))
            else:
                tail = buf

        if tail:
            dst.write(tail.replace(chunk, b""))

    os.replace(temp, path)


def audio_stream_key(stream_id: int) -> int:
    """
    Matches VGMToolbox's filename key construction for multiple audio tracks:
        currentBlockId 0x000001BD
        stream ID stored in the high byte.
    """
    return 0x000001BD | (stream_id << 24)


def audio_extension(stream_id: int) -> Optional[str]:
    # SonyPmfStream.cs:
    #   < 0x20  -> ATRAC3
    #   0x40-0x4F -> LPCM
    #   0x80-0x9E -> subtitles
    if stream_id < 0x20:
        return ".oma"
    return None


AA3_HEADER_CHUNK = bytes([
    0x65, 0x61, 0x33, 0x03, 0x00, 0x00, 0x00, 0x00,
    0x07, 0x76, 0x47, 0x45, 0x4F, 0x42, 0x00, 0x00,
    0x01, 0xC6, 0x00, 0x00, 0x02, 0x62, 0x69, 0x6E,
    0x61, 0x72, 0x79, 0x00, 0x00, 0x00, 0x00, 0x4F,
    0x00, 0x4D, 0x00, 0x47, 0x00, 0x5F, 0x4C, 0x53,
    0x49, 0x00, 0x00, 0x00, 0x01, 0x00, 0x40, 0x00,
    0xDC, 0x00, 0x70, 0x00, 0x08, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x4B, 0x45, 0x59, 0x52, 0x49,
    0x4E, 0x47
])

EA3_HEADER_CHUNK = bytes([
    0x45, 0x41, 0x33, 0x01, 0x00, 0x60, 0xFF, 0xFF
])

AA3_HEADER_SIZE = 0x460
AA3_FORMAT_STRING_LOCATION = 0x420
EA3_HEADER_LOCATION = 0x400


def make_vgmtoolbox_oma_header(first_8_bytes: bytes) -> bytes:
    """
    Exact equivalent of VGMToolbox Atrac3Plus.GetAa3Header(uint headerBlockValue)
    as used by SonyPmfStream.DoFinalTasks().

    VGMToolbox:
        Array.Reverse(headerBytes)
        headerBlock = BitConverter.ToUInt32(headerBytes, 4)
        format = 0x01000000 | (headerBlock & 0xFFFF)
        header[0x400:0x408] = EA3 header
        header[0x420:0x424] = format
        total header size = 0x460 bytes
    """
    if len(first_8_bytes) != 8:
        raise ValueError("ATRAC packet header must be exactly 8 bytes.")

    reversed_header = first_8_bytes[::-1]

    # C# BitConverter.ToUInt32() is little-endian on Windows/.NET.
    header_block = int.from_bytes(reversed_header[4:8], "little")

    format_value = 0x01000000 | (header_block & 0xFFFF)
    format_bytes = format_value.to_bytes(4, "big")

    out = bytearray(AA3_HEADER_SIZE)
    out[0:len(AA3_HEADER_CHUNK)] = AA3_HEADER_CHUNK
    out[EA3_HEADER_LOCATION:EA3_HEADER_LOCATION + len(EA3_HEADER_CHUNK)] = EA3_HEADER_CHUNK
    out[AA3_FORMAT_STRING_LOCATION:AA3_FORMAT_STRING_LOCATION + 4] = format_bytes

    return bytes(out)


def finalize_oma(raw_path: Path, oma_path: Path) -> None:
    """
    Reproduce SonyPmfStream.DoFinalTasks(..., addHeader=true):

      1. Take the first 8 bytes from extracted ATRAC payload.
      2. Remove every occurrence of those 8 bytes from the payload.
      3. Generate the 0x460-byte PSP OMA (AA3/EA3) header.
      4. Prefix the cleaned ATRAC stream with that header.
    """
    with raw_path.open("rb") as f:
        first_8 = f.read(8)

    if len(first_8) != 8:
        raise DemuxError(f"Audio payload is too small to build OMA header: {raw_path}")

    cleaned = raw_path.with_suffix(raw_path.suffix + ".clean")

    # This is the same logical operation as FileUtil.RemoveAllChunksFromFile().
    # Do it streaming so large UMD tracks do not need to fit in RAM.
    tail = b""
    total_removed = 0
    chunk_len = len(first_8)

    with raw_path.open("rb") as src, cleaned.open("wb") as dst:
        while True:
            data = src.read(CHUNK_SIZE)
            if not data:
                break

            buf = tail + data

            if len(buf) >= chunk_len:
                emit_len = len(buf) - (chunk_len - 1)
                emit = buf[:emit_len]
                tail = buf[emit_len:]
                filtered = emit.replace(first_8, b"")
                total_removed += (len(emit) - len(filtered)) // chunk_len
                dst.write(filtered)
            else:
                tail = buf

        if tail:
            filtered = tail.replace(first_8, b"")
            total_removed += (len(tail) - len(filtered)) // chunk_len
            dst.write(filtered)

    oma_header = make_vgmtoolbox_oma_header(first_8)

    with oma_path.open("wb") as out:
        out.write(oma_header)
        with cleaned.open("rb") as inp:
            while True:
                block = inp.read(CHUNK_SIZE)
                if not block:
                    break
                out.write(block)

    cleaned.unlink()
    raw_path.unlink()

    # EA3 signature is the exact thing FFmpeg was missing.
    with oma_path.open("rb") as verify:
        signature = verify.read(4)

    if signature != b"ea3\3":
        raise DemuxError(
            f"Internal error: generated OMA does not start with EA3 signature: "
            f"{signature.hex()}"
        )

    print(f"  Added OMA AA3/EA3 header: {oma_path.name}")
    print(f"  Removed {total_removed} repeated ATRAC packet header(s).")



def demux_mps(mps_path: Path) -> Tuple[Path, List[Path], int, int]:
    """
    Port of SonyPspMpsStream + MpegStream behavior.

    Returns:
        (video_path, atrac_audio_paths, video_packet_count, audio_packet_count)
    """
    folder = mps_path.parent
    stem = mps_path.stem

    video_final = folder / f"{stem}_000001E0.264"
    video_tmp = folder / f"{stem}_000001E0.264.part"

    atrac_paths: Dict[int, Path] = {}
    atrac_tmp_paths: Dict[int, Path] = {}

    video_count = 0
    audio_count = 0

    # Ensure stale partial outputs don't get mistaken for successful output.
    for p in [video_tmp]:
        if p.exists():
            p.unlink()

    with mps_path.open("rb") as src:
        pack_offset = find_first_pack_header(src)
        if pack_offset < 0:
            raise DemuxError("Cannot find MPEG-PS pack header 00 00 01 BA")

        src.seek(pack_offset)

        video_out = video_tmp.open("wb")

        try:
            file_size = mps_path.stat().st_size

            while src.tell() < file_size:
                block_start = src.tell()
                block_id = src.read(4)

                if len(block_id) != 4:
                    break

                if block_id == PACK_START:
                    # MpegStream.cs: Pack Header is a static 0xE bytes.
                    src.seek(block_start + 0x0E)
                    continue

                if block_id == PROGRAM_END:
                    break

                # All handled variable packets have 00 00 01 xx form.
                if block_id[:3] != b"\x00\x00\x01":
                    raise DemuxError(
                        f"Unknown block at 0x{block_start:X}: {block_id.hex()}"
                    )

                code = block_id[3]
                if code not in VARIABLE_CODES:
                    raise DemuxError(
                        f"Block ID at 0x{block_start:X} not found in VGMToolbox packet table: "
                        f"{block_id.hex()}"
                    )

                size_bytes = src.read(2)
                if len(size_bytes) != 2:
                    raise DemuxError(
                        f"Unexpected EOF reading packet size at 0x{block_start:X}"
                    )

                block_size = int.from_bytes(size_bytes, "big")
                payload_base = block_start + 6
                block_end = payload_base + block_size

                if block_end > file_size:
                    raise DemuxError(
                        f"Packet at 0x{block_start:X} extends beyond EOF "
                        f"(end 0x{block_end:X}, file 0x{file_size:X})"
                    )

                # SonyPspMpsStream:
                #   IsThisAnAudioBlock: code == 0xBD
                if code == 0xBD:
                    # GetAudioPacketHeaderSize:
                    #   checkBytes = byte at currentOffset + 8
                    #   header size = checkBytes + 7
                    src.seek(block_start + 8)
                    header_len_byte = src.read(1)
                    if not header_len_byte:
                        raise DemuxError(
                            f"Unexpected EOF reading audio header length at 0x{block_start:X}"
                        )

                    header_len = header_len_byte[0]
                    audio_skip = header_len + 7

                    # GetStreamId:
                    #   sizeValue = byte at currentOffset + 8
                    #   offsetToCheck = sizeValue + 6 + 7 - 4
                    #                = sizeValue + 9
                    stream_id_pos = block_start + header_len + 9
                    if stream_id_pos >= block_end:
                        raise DemuxError(
                            f"Invalid ATRAC stream-id position at 0x{block_start:X}"
                        )

                    src.seek(stream_id_pos)
                    sid_b = src.read(1)
                    if not sid_b:
                        raise DemuxError(
                            f"Unexpected EOF reading ATRAC stream id at 0x{block_start:X}"
                        )

                    stream_id = sid_b[0]
                    ext = audio_extension(stream_id)

                    # Ignore LPCM/subtitle private streams for the requested
                    # .264 + .oma workflow.
                    if ext == ".oma":
                        if stream_id not in atrac_paths:
                            key = audio_stream_key(stream_id)
                            final_path = folder / f"{stem}_{key:08X}.oma"
                            tmp_path = folder / f"{stem}_{key:08X}.oma.part"

                            if tmp_path.exists():
                                tmp_path.unlink()

                            atrac_paths[stream_id] = final_path
                            atrac_tmp_paths[stream_id] = tmp_path

                        # Exact VGMToolbox extraction:
                        # payload starts at currentOffset + 6 + audioSkip
                        payload_start = block_start + 6 + audio_skip
                        payload_len = block_size - audio_skip

                        if payload_len > 0:
                            with atrac_tmp_paths[stream_id].open(
                                "ab"
                            ) as audio_out:
                                copy_range(
                                    src,
                                    audio_out,
                                    payload_start,
                                    payload_len,
                                )
                            audio_count += 1

                # SonyPspMpsStream video block: 0xE0-0xEF.
                elif 0xE0 <= code <= 0xEF:
                    # Mpeg2Stream.GetStandardPesHeaderSize:
                    #   header size = byte at currentOffset + 8 + 3
                    src.seek(block_start + 8)
                    header_len_byte = src.read(1)
                    if not header_len_byte:
                        raise DemuxError(
                            f"Unexpected EOF reading video header length at 0x{block_start:X}"
                        )

                    header_len = header_len_byte[0]
                    video_skip = header_len + 3

                    # SonyPspMpsStream.GetVideoFileExtension checks 4 bytes at
                    # currentOffset + videoHeaderSize + 6 for Annex-B.
                    if video_count == 0:
                        probe_pos = block_start + video_skip + 6
                        if probe_pos + 4 <= file_size:
                            src.seek(probe_pos)
                            probe = src.read(4)
                            if probe != b"\x00\x00\x00\x01":
                                # Keep following VGMToolbox extraction behavior;
                                # just warn instead of aborting.
                                print(
                                    f"  Warning: first video payload does not have "
                                    f"Annex-B start code at 0x{probe_pos:X}."
                                )

                    # Exact VGMToolbox extraction:
                    # payload starts at currentOffset + 6 + videoSkip
                    payload_start = block_start + 6 + video_skip
                    payload_len = block_size - video_skip

                    if payload_len > 0:
                        copy_range(src, video_out, payload_start, payload_len)
                        video_count += 1

                src.seek(block_end)

        finally:
            video_out.close()

    if video_count == 0:
        if video_tmp.exists():
            video_tmp.unlink()
        raise DemuxError("No H.264 video packets were extracted.")

    os.replace(video_tmp, video_final)

    # VGMToolbox's "Extract Audio and Video" path performs its final
    # SonyPmfStream.DoFinalTasks(..., addHeader=true) step here:
    # construct a real PSP OMA/AA3 container with an EA3 header.
    final_audio_paths: List[Path] = []

    for stream_id, tmp_path in atrac_tmp_paths.items():
        final_path = atrac_paths[stream_id]

        if not tmp_path.exists() or tmp_path.stat().st_size == 0:
            if tmp_path.exists():
                tmp_path.unlink()
            continue

        finalize_oma(tmp_path, final_path)
        final_audio_paths.append(final_path)

    # Stable order: first/lowest stream ID is used for MP4, matching the
    # requested "-map 1:a:0" semantics.
    final_audio_paths.sort(
        key=lambda p: int(p.stem.rsplit("_", 1)[1], 16)
    )

    if not final_audio_paths:
        raise DemuxError("No ATRAC3 (.oma) audio stream was extracted.")

    return video_final, final_audio_paths, video_count, audio_count


def run_ffmpeg(
    ffmpeg: str,
    video: Path,
    audio: Path,
    output_mp4: Path,
) -> None:
    """
    Exact requested mux/transcode settings:
        -y -hide_banner
        -i VIDEO
        -i AUDIO
        -map 0:v:0 -map 1:a:0
        -c:v copy
        -c:a aac -b:a 192k
    """
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-i",
        str(video),
        "-i",
        str(audio),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(output_mp4),
    ]

    print()
    print("FFmpeg command:")
    print("  " + subprocess.list2cmdline(cmd))
    print()

    result = subprocess.run(cmd, check=False)

    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed with exit code {result.returncode}"
        )


def process_one(mps_path: Path, ffmpeg: str) -> bool:
    print()
    print("=" * 78)
    print(f"MPS: {mps_path.name}")
    print("=" * 78)

    try:
        video, audios, video_packets, audio_packets = demux_mps(mps_path)

        print(f"Demuxed video : {video.name}")
        print(f"Video packets : {video_packets}")
        print(f"ATRAC streams : {len(audios)}")
        print(f"Audio packets : {audio_packets}")

        for audio in audios:
            print(f"Audio         : {audio.name}")

        # The requested FFmpeg command maps audio stream 0, so use the first
        # extracted ATRAC track.
        audio = audios[0]

        if len(audios) > 1:
            print(
                f"Warning: {len(audios)} ATRAC3 tracks were found; "
                f"using {audio.name} as audio stream 0."
            )

        output_mp4 = mps_path.with_suffix(".mp4")
        run_ffmpeg(ffmpeg, video, audio, output_mp4)

        print(f"SUCCESS: {output_mp4.name}")
        return True

    except Exception as exc:
        print(f"ERROR: {exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Demux PSP UMD Video MPS files and create MP4s."
    )
    parser.add_argument(
        "folder",
        nargs="?",
        help="Folder containing .mps files. Defaults to this script's folder.",
    )
    args = parser.parse_args()

    if args.folder:
        # Windows drag/drop or manually copied commands can sometimes leave
        # a stray quote in the argument. Quotes cannot be part of a valid
        # Windows path, so safely strip surrounding quote characters.
        raw_folder = args.folder.strip()
        while len(raw_folder) >= 2 and raw_folder[0] == '"':
            raw_folder = raw_folder[1:]
        while raw_folder.endswith('"'):
            raw_folder = raw_folder[:-1]
        raw_folder = raw_folder.strip()
        folder = Path(raw_folder).expanduser().resolve()
    else:
        folder = Path(__file__).resolve().parent

    if not folder.is_dir():
        print(f"ERROR: folder does not exist: {folder}")
        return 2

    try:
        ffmpeg = find_ffmpeg()
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        return 2

    mps_files = sorted(
        [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".mps"],
        key=lambda p: p.name.lower(),
    )

    if not mps_files:
        print(f"No .mps files found in: {folder}")
        return 1

    print(f"Folder : {folder}")
    print(f"FFmpeg : {ffmpeg}")
    print(f"MPS    : {len(mps_files)} file(s)")

    success = 0

    for mps in mps_files:
        if process_one(mps, ffmpeg):
            success += 1

    print()
    print("=" * 78)
    print(f"Finished: {success}/{len(mps_files)} succeeded.")
    print("=" * 78)

    return 0 if success == len(mps_files) else 1


if __name__ == "__main__":
    sys.exit(main())
