"""Replaceable Korean TTS engines and measured, sentence-aligned PCM audio.

The default engine deliberately produces silence, not synthesized speech. Set
MFLOW_TTS_ENGINE=sapi to explicitly use an installed Windows Korean SAPI voice.
OpenAI speech requires an explicit engine selection and a server API key.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Protocol
import urllib.error
import urllib.request
import wave


class TTSEngine(Protocol):
    name: str
    mock: bool

    def synthesize(self, text: str, output_path: Path) -> None:
        """Write one sentence as an uncompressed PCM WAV file."""
        ...


class MockTTS:
    """Deterministic silent test audio; durations are estimates until measured."""

    name = 'mock-silence'
    mock = True

    def synthesize(self, text: str, output_path: Path) -> None:
        duration = max(0.4, len(''.join(text.split())) / 6.0)
        with wave.open(str(output_path), 'wb') as audio:
            audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\x00\x00' * round(duration * 24000))


class WindowsSAPI:
    """Offline speech using a Korean voice installed on Windows.

    Input text is read from a UTF-8 file, never interpolated as shell code.
    A missing Korean voice is an error; we do not silently substitute a voice.
    """

    name = 'windows-sapi-ko-KR'
    mock = False

    def __init__(self, voice='', rate=0):
        self.voice = voice
        self.rate = rate

    code = r'''
param([string]$TextPath, [string]$AudioPath, [string]$VoiceName, [int]$Rate)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $voice = $speaker.GetInstalledVoices() | Where-Object {
        $_.Enabled -and $_.VoiceInfo.Culture.Name -eq 'ko-KR' -and
            (!$VoiceName -or $_.VoiceInfo.Name -eq $VoiceName)
    } | Select-Object -First 1
    if ($null -eq $voice) { throw 'Korean SAPI voice is not installed.' }
    $speaker.SelectVoice($voice.VoiceInfo.Name)
    $speaker.Rate = $Rate
    $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(24000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
    $speaker.SetOutputToWaveFile($AudioPath, $format)
    $speaker.Speak([System.IO.File]::ReadAllText($TextPath, [System.Text.Encoding]::UTF8))
} finally { $speaker.Dispose() }
'''

    def synthesize(self, text: str, output_path: Path) -> None:
        if os.name != 'nt':
            raise ValueError('Windows 음성 엔진은 Windows에서만 사용할 수 있습니다.')
        with tempfile.TemporaryDirectory(prefix='mflow-sapi-') as scratch:
            folder = Path(scratch)
            text_path, script_path = folder / 'text.txt', folder / 'speak.ps1'
            text_path.write_text(text, encoding='utf-8')
            script_path.write_text(self.code, encoding='utf-8-sig')
            try:
                result = subprocess.run(
                    ['powershell.exe', '-NoProfile', '-NonInteractive',
                     '-ExecutionPolicy', 'Bypass', '-File', str(script_path),
                     '-TextPath', str(text_path), '-AudioPath', str(output_path.resolve()),
                     '-VoiceName', self.voice, '-Rate', str(self.rate)],
                    capture_output=True, timeout=90,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ValueError('Windows 한국어 음성 생성에 실패했습니다. 음성 설치 상태를 확인하세요.') from exc
            if result.returncode or not output_path.is_file():
                raise ValueError('선택한 엔진의 Windows 한국어 음성을 사용할 수 없습니다.')


class WindowsOneCore(WindowsSAPI):
    """Use installed Windows speech voices exposed through WinRT."""

    name = 'windows-onecore-ko-KR'
    code = r'''
param([string]$TextPath, [string]$AudioPath, [string]$VoiceName, [int]$Rate)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.SpeechSynthesis.SpeechSynthesizer, Windows.Media.SpeechSynthesis, ContentType=WindowsRuntime]
$resultType = [Windows.Media.SpeechSynthesis.SpeechSynthesisStream, Windows.Media.SpeechSynthesis, ContentType=WindowsRuntime]
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and
    $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
$voice = [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices | Where-Object {
    $_.Language -eq 'ko-KR' -and (!$VoiceName -or $_.DisplayName -eq $VoiceName)
} | Select-Object -First 1
if ($null -eq $voice) { throw 'Korean OneCore voice is not installed.' }
$speaker = New-Object Windows.Media.SpeechSynthesis.SpeechSynthesizer
try {
    $speaker.Voice = $voice
    if ($Rate) { $speaker.Options.SpeakingRate = 1.0 + $Rate * 0.1 }
    $text = [System.IO.File]::ReadAllText($TextPath, [System.Text.Encoding]::UTF8)
    $operation = $speaker.SynthesizeTextToStreamAsync($text)
    $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
    $stream = $task.GetAwaiter().GetResult()
    $inputStream = [System.IO.WindowsRuntimeStreamExtensions]::AsStreamForRead($stream)
    try {
        $outputStream = [System.IO.File]::Create($AudioPath)
        try { $inputStream.CopyTo($outputStream) }
        finally { $outputStream.Dispose() }
    } finally { $inputStream.Dispose() }
} finally { $speaker.Dispose() }
'''


def openai_api_key():
    return os.environ.get('MFLOW_AI_KEY') or os.environ.get('OPENAI_API_KEY')


class OpenAITTS:
    """Request WAV for each approved sentence; the caller measures the PCM."""

    mock = False

    def __init__(self):
        if not openai_api_key():
            raise ValueError('OpenAI 음성에는 서버 API 키 설정이 필요합니다.')
        self.model = os.environ.get('MFLOW_TTS_MODEL', 'gpt-4o-mini-tts')
        self.voice = os.environ.get('MFLOW_TTS_VOICE', 'alloy')
        self.name = f'openai:{self.model}:{self.voice}'

    def synthesize(self, text, output_path):
        key = openai_api_key()
        if not key:
            raise ValueError('OpenAI 음성에는 서버 API 키 설정이 필요합니다.')
        payload = {'model': self.model, 'voice': self.voice, 'input': text, 'response_format': 'wav'}
        request = urllib.request.Request(
            'https://api.openai.com/v1/audio/speech', data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
        )
        limit = 20 * 1024 * 1024
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                audio = response.read(limit + 1)
        except (urllib.error.URLError, TimeoutError, ValueError):
            raise ValueError('OpenAI 음성 요청에 실패했습니다. 키·할당량·네트워크 설정을 확인하세요.') from None
        if len(audio) > limit:
            raise ValueError('OpenAI 음성 응답이 최대 파일 크기를 초과했습니다.')
        output_path.write_bytes(audio)


def configured_engine(name=None) -> TTSEngine:
    name = (name if name is not None else os.environ.get('MFLOW_TTS_ENGINE', 'mock')).strip().lower()
    if name == 'mock':
        return MockTTS()
    if name == 'sapi':
        return WindowsSAPI()
    if name == 'onecore':
        return WindowsOneCore()
    if name == 'openai':
        return OpenAITTS()
    raise ValueError('MFLOW_TTS_ENGINE은 mock, sapi, onecore, openai 중 하나여야 합니다.')


def script_fingerprint(script: dict) -> str:
    """Bind generated audio to the exact approved sentence sequence."""
    value = json.dumps(script.get('sentences'), ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def synthesize(script: dict, output_dir, engine: TTSEngine | None = None) -> dict:
    """Generate and join sentence WAVs; all timings come from actual frame counts.

    The caller owns approval authentication and should invalidate narration when
    its project or script changes. This boundary also refuses unapproved scripts.
    Existing output is replaced only after every sentence has been synthesized.
    """
    if not isinstance(script, dict) or script.get('approved') is not True:
        raise ValueError('승인된 대본만 음성으로 만들 수 있습니다.')
    sentences = script.get('sentences')
    if not isinstance(sentences, list) or not 1 <= len(sentences) <= 200:
        raise ValueError('대본에는 1~200개의 문장이 필요합니다.')
    seen = set()
    for sentence in sentences:
        if not isinstance(sentence, dict):
            raise ValueError('대본 문장 형식이 올바르지 않습니다.')
        ident, scene_id, text = (sentence.get(key) for key in ('id', 'scene_id', 'text'))
        if not isinstance(ident, str) or not ident.strip() or ident in seen:
            raise ValueError('대본 문장 ID는 비어 있지 않고 고유해야 합니다.')
        if not isinstance(scene_id, str) or not scene_id.strip():
            raise ValueError('대본 문장에는 연결된 장면 ID가 필요합니다.')
        if not isinstance(text, str) or not text.strip() or len(text) > 1000:
            raise ValueError('각 나레이션 문장은 1~1000자여야 합니다.')
        seen.add(ident)
    engine = engine if engine is not None else configured_engine()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='tts-', dir=output_dir) as scratch:
        staging = Path(scratch)
        total_frames, format_key, segments = 0, None, []
        joined = staging / 'narration.wav'
        with wave.open(str(joined), 'wb') as combined:
            # Keep error cleanup valid even when the first provider call fails.
            combined.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            for index, sentence in enumerate(sentences):
                path = staging / f'{index:04d}.wav'
                engine.synthesize(sentence['text'], path)
                try:
                    with wave.open(str(path), 'rb') as audio:
                        key = (audio.getnchannels(), audio.getsampwidth(), audio.getframerate())
                        frames = audio.getnframes()
                        if audio.getcomptype() != 'NONE' or key[0] not in (1, 2) or key[1] not in (1, 2, 3, 4) or key[2] <= 0 or frames <= 0:
                            raise ValueError('TTS 출력은 비어 있지 않은 PCM WAV여야 합니다.')
                        if frames / key[2] > 180:
                            raise ValueError('한 문장의 음성은 180초를 넘을 수 없습니다.')
                        if format_key is None:
                            format_key = key
                            combined.setparams((*key, 0, 'NONE', 'not compressed'))
                        elif format_key != key:
                            raise ValueError('문장별 TTS 오디오 형식이 일치하지 않습니다.')
                        pcm = audio.readframes(frames)
                        if len(pcm) != frames * key[0] * key[1]:
                            raise ValueError('TTS WAV 파일이 잘렸습니다.')
                except (OSError, EOFError, wave.Error) as exc:
                    raise ValueError('TTS 엔진이 유효한 PCM WAV를 만들지 못했습니다.') from exc
                start = total_frames / key[2]
                total_frames += frames
                end = total_frames / key[2]
                if not math.isfinite(end) or end > 600:
                    raise ValueError('전체 나레이션은 600초를 넘을 수 없습니다.')
                combined.writeframes(pcm)
                segments.append({'sentence_id': sentence['id'], 'scene_id': sentence['scene_id'],
                                 'text': sentence['text'], 'start': round(start, 6),
                                 'end': round(end, 6), 'duration': round(frames / key[2], 6)})
        result = {'engine': engine.name, 'mock': bool(engine.mock), 'audio_file': 'narration.wav',
                  'duration': round(total_frames / format_key[2], 6), 'segments': segments,
                  'script_fingerprint': script_fingerprint(script)}
        manifest = staging / 'narration.json'
        manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        joined.replace(output_dir / 'narration.wav')
        manifest.replace(output_dir / 'narration.json')
    return result
