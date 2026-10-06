UMD MPS -> MP4 (Windows 10)

Files:
    umd_mps_to_mp4.py
    run_umd_mps_to_mp4.bat

Requirements:
    - Windows 10
    - Python 3
    - ffmpeg.exe installed and on PATH
      OR ffmpeg.exe placed beside umd_mps_to_mp4.py

Usage:
    1. Put the .MPS files in a folder.
    2. Drag the folder onto run_umd_mps_to_mp4.bat
       OR use CMD:

         python umd_mps_to_mp4.py "C:\path\to\folder"

For every .MPS:
    <name>_000001E0.264  = extracted H.264 video
    <name>_000001BD.oma  = first ATRAC3 audio stream
    <name>.mp4            = final output

The final FFmpeg step is:

    ffmpeg.exe -y -hide_banner ^
        -i "VIDEO.264" ^
        -i "AUDIO.oma" ^
        -map 0:v:0 -map 1:a:0 ^
        -c:v copy ^
        -c:a aac -b:a 192k ^
        "OUTPUT.mp4"

Video is stream-copied (not re-encoded).
Audio is encoded to AAC 192 kb/s.

The MPS demuxer is a Python port of the packet handling used by
VGMToolbox's SonyPspMpsStream/SonyPmfStream/MpegStream classes.

Important:
    The OMA output is finalized with the PSP AA3/EA3 header used by
    VGMToolbox's SonyPmfStream.DoFinalTasks(addHeader=true). A raw ATRAC
    payload merely renamed ".oma" is NOT a valid OMA container and FFmpeg
    will report "Couldn't find the EA3 header".
