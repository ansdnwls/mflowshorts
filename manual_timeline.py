"""Validate ordered source cuts and timed text on the output timeline."""
import math


GRADE_DEFAULTS = {'brightness':0, 'contrast':0, 'saturation':0, 'warmth':0}
GRADE_RANGES = {'brightness':(-40,40), 'contrast':(-50,50),
                'saturation':(-100,100), 'warmth':(0,30)}


def color_grade(value):
    if not isinstance(value,dict):
        raise ValueError('클립 화면 보정 값을 확인하세요.')
    result={}
    for key,(low,high) in GRADE_RANGES.items():
        number=value.get(key,GRADE_DEFAULTS[key])
        if type(number) not in (int,float) or not low<=number<=high:
            raise ValueError('클립 화면 보정 값이 범위를 벗어났습니다.')
        result[key]=round(number)
    return result


def framing(value):
    if not isinstance(value, dict) or value.get('mode') not in ('default', 'fit', 'fill'):
        raise ValueError('클립 배치 방식을 확인하세요.')
    result = {'mode': value['mode']}
    for key, minimum, maximum in (('zoom', 1, 3), ('x', -1080, 1080), ('y', -1470, 1470)):
        number = value.get(key)
        if type(number) not in (int, float) or not minimum <= number <= maximum:
            raise ValueError('클립 확대 또는 위치 값이 범위를 벗어났습니다.')
        result[key] = round(number, 2) if key == 'zoom' else round(number)
    return result


def seconds(value):
    if type(value) not in (int, float) or not 0 <= value <= 5400 or not math.isfinite(value):
        raise ValueError('시간은 0 이상의 초 단위 숫자로 입력하세요.')
    return round(value, 3)


def clips(value, source_duration):
    if not isinstance(value, list) or len(value) > 40:
        raise ValueError('영상 구간은 최대 40개입니다.')
    result = []
    for cut in value:
        if not isinstance(cut, dict):
            raise ValueError('영상 구간 형식을 확인하세요.')
        start, end = seconds(cut.get('start')), seconds(cut.get('end'))
        if end > source_duration or round(end-start, 3) < 0.1:
            raise ValueError('각 구간은 원본 범위 안에서 0.1초 이상이어야 합니다.')
        clip = {'start': start, 'end': end}
        if 'framing' in cut:
            clip['framing'] = framing(cut['framing'])
        if 'grade' in cut:
            grade=color_grade(cut['grade'])
            if grade!=GRADE_DEFAULTS:
                clip['grade']=grade
        result.append(clip)
    if duration(result) > 60:
        raise ValueError('편집 영상은 최대 60초입니다.')
    return result


def duration(cuts):
    return round(sum(cut['end']-cut['start'] for cut in cuts), 3)


def quotes(value):
    if not isinstance(value, list) or len(value) > 40:
        raise ValueError('글귀는 최대 40개입니다.')
    result = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get('text'), str):
            raise ValueError('글귀 내용을 입력하세요.')
        text = ' '.join(item['text'].split())
        start, end = seconds(item.get('start')), seconds(item.get('end'))
        if not 1 <= len(text) <= 60 or not 0 <= start < 60 or end > 60 or (end and end <= start):
            raise ValueError('글귀는 1~60자이고, 끝은 시작보다 뒤여야 합니다. 끝이 0이면 영상 끝까지 표시됩니다.')
        result.append({'text': text, 'start': start, 'end': end})
    result.sort(key=lambda item: item['start'])
    if any((a['end'] or 60) > b['start'] for a,b in zip(result,result[1:])):
        raise ValueError('글귀 표시 시간이 서로 겹칩니다.')
    return result


def clip_at(cuts, output_time):
    for cut in cuts:
        span=round(cut['end']-cut['start'], 3)
        if output_time < span:
            return cut, cut['start']+output_time
        output_time=round(output_time-span, 9)
    return cuts[-1], cuts[-1]['end']


def source_time(cuts, output_time):
    return clip_at(cuts, output_time)[1]


def graph(cuts, audio, frame_filters=None):
    """Build one FFmpeg graph without seeking or re-opening the input for each clip."""
    count=len(cuts)
    filters=['[0:v:0]fps=30,split='+str(count)+''.join(f'[mv{i}]' for i in range(count))]
    if audio:
        filters.append('[0:a:0]asplit='+str(count)+''.join(f'[ma{i}]' for i in range(count)))
    elapsed=0
    for i,cut in enumerate(cuts):
        start,end=cut['start'],cut['end']
        span=end-start
        frames=round((elapsed+span)*30)-round(elapsed*30)
        elapsed+=span
        first=round(start*30)
        framing = ',' + ','.join(frame_filters(cut)) if frame_filters else ''
        filters.append(f'[mv{i}]trim=start_frame={first}:end_frame={round(end*30)},setpts=PTS-STARTPTS,'
                       f'tpad=stop_mode=clone:stop=2,trim=end_frame={frames}{framing}[vcut{i}]')
        if audio:
            filters.append(f'[ma{i}]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS,'
                           f'apad=whole_dur={span:.6f},atrim=duration={span:.6f}[acut{i}]')
    filters.append(''.join(f'[vcut{i}]' for i in range(count))+f'concat=n={count}:v=1:a=0[cut_video]')
    if audio:
        filters.append(''.join(f'[acut{i}]' for i in range(count))+f'concat=n={count}:v=0:a=1[cut_audio]')
    return {'filters':filters,'video_label':'cut_video','audio_label':'cut_audio' if audio else None}
