"""Decode bounded audio ranges into time-aligned min/max bins."""
from array import array
import math
import subprocess
import sys
import threading

RATE = 8000
BIN = 160  # 20 ms at the decoding rate
SLOTS = threading.BoundedSemaphore(2)


def peaks(path, formats, start, duration):
    try:
        start, duration = float(start), float(duration)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('파형 시간 범위를 확인하세요.') from None
    if not 0 <= start <= 5400 or not 0.001 <= duration <= 60:
        raise ValueError('파형 시간 범위를 확인하세요.')
    command = ['ffmpeg','-hide_banner','-loglevel','error','-nostdin',
               '-ss',f'{start:.3f}','-format_whitelist',formats,'-protocol_whitelist','file',
               '-i',str(path),'-map','0:a:0','-vn','-ac','1',
               '-af',f'aresample={RATE}:async=1:first_pts=0,apad',
               '-t',f'{duration:.3f}','-f','s16le','-']
    if not SLOTS.acquire(timeout=3):
        raise ValueError('파형을 처리 중입니다. 잠시 후 다시 시도하세요.')
    try:
        raw=subprocess.run(command,capture_output=True,check=True,timeout=20).stdout
    except (OSError,subprocess.SubprocessError):
        raise ValueError('오디오 파형을 읽을 수 없습니다.') from None
    finally:
        SLOTS.release()
    samples=array('h',raw)
    if sys.byteorder!='little':
        samples.byteswap()
    count=math.ceil(duration*RATE/BIN)
    result=[]
    for offset in range(count):
        chunk=samples[offset*BIN:(offset+1)*BIN]
        result.append([round(min(chunk,default=0)/32768,3),round(max(chunk,default=0)/32768,3)])
    return {'step':BIN/RATE,'duration':duration,'peaks':result}
