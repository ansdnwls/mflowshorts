"""Erase only the letters of burned-in text inside boxes (OpenCV inpainting).

Runs in the media venv (needs opencv-python-headless and numpy). JSON on stdin:
  {"mode": "video", "source", "start", "duration", "chain", "boxes", "output", "width", "height"}
  {"mode": "frame", ... same without output, "at"}  -> PNG bytes on stdout
Boxes use 0~1 coordinates of the laid-out frame and 'start'/'end' seconds (end 0 = to the end).
"""
import json
import subprocess
import sys

FPS = 30


def text_mask(region, cv2, np):
    """Pixels that belong to text strokes and their outline, not the background around them.

    Subtitles are thin bright strokes with a dark outline (or the reverse), so top-hat and black-hat
    pick them out against smoother backgrounds. The mask is grown a little to cover anti-aliasing.
    """
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    size = max(9, min(31, region.shape[0] // 2 | 1))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    bright = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kernel)
    dark = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, kernel)
    strokes = np.maximum(bright, dark)
    _, mask = cv2.threshold(strokes, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if cv2.countNonZero(mask) < mask.size * 0.01:  # no clear text here: leave the frame alone
        return None
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    return cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)))


def clean(frame, boxes, t, cv2, np):
    height, width = frame.shape[:2]
    for box in boxes:
        if t < box['start'] or (box['end'] and t >= box['end']):
            continue
        x, y = int(box['x'] * width), int(box['y'] * height)
        w, h = int(box['w'] * width), int(box['h'] * height)
        if w < 8 or h < 8:
            continue
        # Inpaint with a margin of real pixels around the box so the fill matches the surroundings.
        m = 12
        x0, y0, x1, y1 = max(x - m, 0), max(y - m, 0), min(x + w + m, width), min(y + h + m, height)
        region = frame[y0:y1, x0:x1]
        # Look for strokes a little outside the box too, so outline pixels the box just misses are not smeared in.
        g = 6
        gx0, gy0, gx1, gy1 = max(x - g, 0), max(y - g, 0), min(x + w + g, width), min(y + h + g, height)
        inner = text_mask(frame[gy0:gy1, gx0:gx1], cv2, np)
        if inner is None:
            continue
        mask = np.zeros(region.shape[:2], np.uint8)
        mask[gy0 - y0:gy1 - y0, gx0 - x0:gx1 - x0] = inner
        frame[y0:y1, x0:x1] = cv2.inpaint(region, mask, 5, cv2.INPAINT_TELEA)
    return frame


def decode(task, extra):
    return ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-ss', str(task['start']),
            '-format_whitelist', task['formats'], '-protocol_whitelist', 'file', '-i', task['source'],
            *extra, '-vf', task['chain'] + f',fps={FPS},format=bgr24', '-f', 'rawvideo', '-']


def run(task):
    import cv2
    import numpy as np
    width, height = task['width'], task['height']
    size = width * height * 3
    if task['mode'] == 'frame':
        raw = subprocess.run(decode({**task, 'start': task['start'] + task['at']}, ['-frames:v', '1']), capture_output=True, timeout=60).stdout
        if len(raw) < size:
            raise ValueError('frame')
        frame = np.frombuffer(raw[:size], np.uint8).reshape(height, width, 3).copy()
        boxes = [{**b, 'start': 0, 'end': 0} for b in task['boxes'] if b['start'] <= task['at'] and (not b['end'] or task['at'] < b['end'])]
        ok, png = cv2.imencode('.png', clean(frame, boxes, 0, cv2, np))
        sys.stdout.buffer.write(png.tobytes())
        return
    reader = subprocess.Popen(decode(task, ['-t', str(task['duration'])]), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    writer = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', '-f', 'rawvideo', '-pix_fmt', 'bgr24',
                               '-s', f'{width}x{height}', '-r', str(FPS), '-i', '-', '-c:v', 'libx264', '-preset', 'veryfast',
                               '-crf', '14', '-pix_fmt', 'yuv420p', task['output']], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    index = 0
    try:
        while True:
            raw = reader.stdout.read(size)
            if len(raw) < size:
                break
            frame = np.frombuffer(raw, np.uint8).reshape(height, width, 3).copy()
            writer.stdin.write(clean(frame, task['boxes'], index / FPS, cv2, np).tobytes())
            index += 1
    finally:
        writer.stdin.close()
        reader.stdout.close()
        reader.wait()
        writer.wait()
    if not index or writer.returncode:
        raise ValueError('encode')


if __name__ == '__main__':
    run(json.loads(sys.stdin.buffer.read().decode('utf-8')))  # Windows defaults to a legacy code page
