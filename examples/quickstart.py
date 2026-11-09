"""The README quickstart as a script: upload a file, wait, print the transcript and the first SRT cue.

    SCRIPTIVOX_API_KEY=sk_live_... python examples/quickstart.py [path-to-audio]
"""
import sys
from pathlib import Path

from scriptivox import Scriptivox

file = sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent.parent / "tests" / "fixtures" / "speech-en.mp3"

with Scriptivox() as client:
    job = client.transcribe(file=file, language="en", on_upload_progress=lambda p: print(f"\ruploading {p.percent}%", end=""))
    print(f"\njob {job['id']} {job['status']}")
    done = client.wait(job["id"], on_poll=lambda t: print(f"  {t['status']}"))
    print(f"\n{done['result']['full_transcript']}\n")
    print(client.get(job["id"], format="srt").split("\n\n")[0])
