# mflowshorts Windows 설치·점검 스크립트
#
# 실행 (프로젝트 폴더에서):
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_windows.ps1
#   scripts\install_windows.cmd              (같은 동작, cmd에서 실행)
#   scripts\install_windows.cmd -CheckOnly   (아무것도 설치하지 않고 상태만 확인)
#
# 확인 항목: Python 3.11+ (requirements-media.lock의 multidict 7.2.1이 3.11 이상 필요), Git, FFmpeg·ffprobe, 맑은 고딕 Bold, 미디어 가상환경(yt-dlp·Playwright),
# douyin-downloader 고정 커밋, Playwright Chromium, 한글 제목·자막 실제 렌더링(앱과 같은 ASS/libass 경로).
# 없는 프로그램은 winget으로 현재 사용자 범위에 설치합니다. 기존 data/ 내용은 지우지 않습니다.
[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [switch]$SkipMedia
)

# 외부 프로그램은 종료 코드로 판단합니다. (Windows PowerShell 5.1은 stderr 출력만으로 Stop 오류를 낼 수 있음)
$ErrorActionPreference = 'Continue'
Set-StrictMode -Version 2.0
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $ProjectRoot

$VendorCommit = '1e08f03b8909b364fb6237d8f9f441e3fbf3d427'
$VendorPath = Join-Path $ProjectRoot 'data\vendor\douyin-downloader'
$VenvPath = Join-Path $ProjectRoot 'data\media-venv'
$VenvPython = Join-Path $VenvPath 'Scripts\python.exe'

$Results = New-Object System.Collections.Generic.List[object]
function Add-Result([string]$Name, [string]$State, [string]$Detail) {
    $Results.Add([pscustomobject]@{ 항목 = $Name; 상태 = $State; 내용 = $Detail })
    $color = @{ OK = 'Green'; 경고 = 'Yellow'; 실패 = 'Red'; 건너뜀 = 'DarkGray' }[$State]
    Write-Host ("[{0}] {1}: {2}" -f $State, $Name, $Detail) -ForegroundColor $color
}
function Write-Step([string]$Text) { Write-Host ''; Write-Host "== $Text" -ForegroundColor Cyan }

function Update-SessionPath {
    # winget이 바꾼 PATH를 새 창을 열지 않고 현재 세션에 반영합니다.
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $links = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'
    $env:Path = (@($machine, $user, $links) | Where-Object { $_ }) -join ';'
}

function Install-WithWinget([string]$Id, [string]$Label) {
    if ($CheckOnly) { return $false }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "winget이 없어 $Label 을(를) 자동 설치할 수 없습니다. Microsoft Store의 '앱 설치 관리자'를 설치하거나 직접 설치하세요." -ForegroundColor Yellow
        return $false
    }
    Write-Host "winget으로 $Label 설치 중 ($Id)..."
    & winget install --exact --id $Id --scope user --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
    if ($LASTEXITCODE -ne 0) {
        # 사용자 범위 설치 패키지가 없으면 기본 범위로 한 번 더 시도합니다.
        & winget install --exact --id $Id --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
    }
    Update-SessionPath
    return $true
}

function Invoke-PythonCode([string]$Exe, [string[]]$Prefix, [string]$Code, [string[]]$Arguments) {
    # 코드를 임시 .py 파일로 넘깁니다. PowerShell 5.1은 -c 인자 안의 큰따옴표를 망가뜨립니다.
    $file = Join-Path ([IO.Path]::GetTempPath()) ("mflow_check_{0}.py" -f [guid]::NewGuid().ToString('N'))
    [IO.File]::WriteAllText($file, $Code, (New-Object Text.UTF8Encoding $false))
    try { return @(& $Exe @Prefix $file @Arguments) }
    finally { Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue }
}

function Find-Python {
    # Microsoft Store 바로가기(python.exe 대체 실행 파일)는 출력 없이 실패하므로 자연스럽게 제외됩니다.
    $probe = "import sys`nprint('%d.%d' % sys.version_info[:2])`nprint(sys.executable)`n"
    foreach ($candidate in @('py -3', 'python', 'python3')) {
        $parts = $candidate.Split(' ')
        $exe = $parts[0]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $prefix = @($parts | Select-Object -Skip 1)
        try { $out = @(Invoke-PythonCode $exe $prefix $probe @() 2>$null) } catch { continue }
        if ($LASTEXITCODE -ne 0 -or $out.Count -lt 2) { continue }
        $parts = $out[0].Trim().Split('.')
        if ([int]$parts[0] -gt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 11)) {
            return [pscustomobject]@{ Version = $out[0].Trim(); Path = $out[1].Trim() }
        }
    }
    return $null
}

function Get-FirstLine([string]$Exe, [string[]]$Arguments) {
    try { $out = @(& $Exe @Arguments 2>$null) } catch { return $null }
    if ($LASTEXITCODE -ne 0 -or $out.Count -eq 0) { return $null }
    return ([string]$out[0]).Trim()
}

Write-Host "mflowshorts Windows 설치 점검: $ProjectRoot"
if ($CheckOnly) { Write-Host '확인 전용 모드: 아무것도 설치하거나 바꾸지 않습니다.' -ForegroundColor Yellow }
Update-SessionPath

# 1. Python ---------------------------------------------------------------
Write-Step 'Python 3.11 이상'
$Python = Find-Python
if (-not $Python -and (Install-WithWinget 'Python.Python.3.11' 'Python 3.11')) { $Python = Find-Python }
if ($Python) { Add-Result 'Python' 'OK' "$($Python.Version) ($($Python.Path))" }
else { Add-Result 'Python' '실패' 'Python 3.11 이상을 찾지 못했습니다. python.org 설치 시 "Add python.exe to PATH"를 선택하세요.' }

# 2. Git (douyin-downloader 내려받기용) -------------------------------------
Write-Step 'Git'
$gitVersion = Get-FirstLine 'git' @('--version')
if (-not $gitVersion -and (Install-WithWinget 'Git.Git' 'Git')) { $gitVersion = Get-FirstLine 'git' @('--version') }
if ($gitVersion) { Add-Result 'Git' 'OK' $gitVersion }
elseif ($SkipMedia) { Add-Result 'Git' '건너뜀' '미디어 도구를 건너뛰어 필요하지 않습니다.' }
else { Add-Result 'Git' '실패' 'Git이 없어 Douyin 도구를 받을 수 없습니다. https://git-scm.com 에서 설치하세요.' }

# 3. FFmpeg / ffprobe -----------------------------------------------------
Write-Step 'FFmpeg · ffprobe'
$ffmpeg = Get-FirstLine 'ffmpeg' @('-hide_banner', '-version')
$ffprobe = Get-FirstLine 'ffprobe' @('-hide_banner', '-version')
if ((-not $ffmpeg -or -not $ffprobe) -and (Install-WithWinget 'Gyan.FFmpeg' 'FFmpeg')) {
    $ffmpeg = Get-FirstLine 'ffmpeg' @('-hide_banner', '-version')
    $ffprobe = Get-FirstLine 'ffprobe' @('-hide_banner', '-version')
}
if ($ffmpeg) { Add-Result 'FFmpeg' 'OK' "$ffmpeg ($((Get-Command ffmpeg).Source))" }
else { Add-Result 'FFmpeg' '실패' 'PATH에서 ffmpeg를 찾지 못했습니다. 설치 후 새 터미널을 열어 다시 실행하세요.' }
if ($ffprobe) { Add-Result 'ffprobe' 'OK' $ffprobe }
else { Add-Result 'ffprobe' '실패' 'PATH에서 ffprobe를 찾지 못했습니다.' }

# 4. 한글 글꼴 ------------------------------------------------------------
Write-Step '한글 글꼴 (맑은 고딕 Bold)'
# production.py는 MFLOW_FONT가 없으면 C:/Windows/Fonts/malgunbd.ttf를 사용합니다.
$appFont = 'C:\Windows\Fonts\malgunbd.ttf'
$systemFont = Join-Path $env:WINDIR 'Fonts\malgunbd.ttf'
if ($env:MFLOW_FONT) {
    if (Test-Path -LiteralPath $env:MFLOW_FONT -PathType Leaf) { Add-Result '글꼴' 'OK' "MFLOW_FONT=$($env:MFLOW_FONT)" }
    else { Add-Result '글꼴' '실패' "MFLOW_FONT 경로에 파일이 없습니다: $($env:MFLOW_FONT)" }
}
elseif (Test-Path -LiteralPath $appFont -PathType Leaf) { Add-Result '글꼴' 'OK' $appFont }
elseif (Test-Path -LiteralPath $systemFont -PathType Leaf) {
    Add-Result '글꼴' '경고' "글꼴은 $systemFont 에 있습니다. 실행 전에 set MFLOW_FONT=$systemFont 를 지정하세요."
}
else { Add-Result '글꼴' '실패' '맑은 고딕 Bold(malgunbd.ttf)가 없습니다. 설정 > 시스템 > 선택적 기능에서 한국어 보조 글꼴을 추가하거나 MFLOW_FONT에 다른 한글 글꼴 경로를 지정하세요.' }

# 5. 미디어 도구 (Douyin·TikTok) -------------------------------------------
Write-Step '미디어 도구 (yt-dlp · Playwright · douyin-downloader)'
if ($SkipMedia) {
    Add-Result '미디어 가상환경' '건너뜀' '-SkipMedia 지정'
    Add-Result 'Chromium' '건너뜀' '-SkipMedia 지정'
}
else {
    # douyin-downloader: 고정 커밋만 사용하고, 기존 폴더는 절대 덮어쓰지 않습니다.
    if (Test-Path -LiteralPath (Join-Path $VendorPath '.git')) {
        $head = Get-FirstLine 'git' @('-C', $VendorPath, 'rev-parse', 'HEAD')
        if ($head -eq $VendorCommit) { Add-Result 'douyin-downloader' 'OK' "커밋 $($VendorCommit.Substring(0, 12))" }
        else { Add-Result 'douyin-downloader' '실패' "다른 커밋($head)이 있어 그대로 두었습니다. 확인 후 정리하세요." }
    }
    elseif (Test-Path -LiteralPath $VendorPath) {
        Add-Result 'douyin-downloader' '실패' "Git 저장소가 아닌 $VendorPath 가 이미 있어 그대로 두었습니다."
    }
    elseif ($CheckOnly) { Add-Result 'douyin-downloader' '경고' '아직 설치되지 않았습니다.' }
    elseif (-not $gitVersion) { Add-Result 'douyin-downloader' '실패' 'Git이 없어 받지 못했습니다.' }
    else {
        New-Item -ItemType Directory -Force -Path (Split-Path $VendorPath) | Out-Null
        & git clone --no-checkout https://github.com/jiji262/douyin-downloader.git $VendorPath
        if ($LASTEXITCODE -eq 0) { & git -C $VendorPath checkout --detach $VendorCommit }
        if ($LASTEXITCODE -eq 0) { Add-Result 'douyin-downloader' 'OK' "커밋 $($VendorCommit.Substring(0, 12)) 설치" }
        else { Add-Result 'douyin-downloader' '실패' 'git clone/checkout에 실패했습니다. 네트워크를 확인하세요.' }
    }

    # 가상환경 + 고정 버전 패키지
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        if ($CheckOnly) { Add-Result '미디어 가상환경' '경고' "아직 없습니다: $VenvPath" }
        elseif ($Python) { & $Python.Path -m venv $VenvPath }
    }
    if ((Test-Path -LiteralPath $VenvPython) -and -not $CheckOnly) {
        & $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $ProjectRoot 'requirements-media.lock')
        if ($LASTEXITCODE -eq 0) { & $VenvPython -m pip check }
        if ($LASTEXITCODE -ne 0) { Add-Result '미디어 가상환경' '실패' 'pip 설치 또는 의존성 확인에 실패했습니다. 위 오류를 확인하세요.' }
    }
    if (Test-Path -LiteralPath $VenvPython) {
        $ytdlp = Get-FirstLine $VenvPython @('-m', 'yt_dlp', '--version')
        if ($ytdlp) { Add-Result '미디어 가상환경' 'OK' "yt-dlp $ytdlp ($VenvPython)" }
        elseif (-not ($Results | Where-Object { $_.항목 -eq '미디어 가상환경' })) { Add-Result '미디어 가상환경' '실패' 'yt-dlp를 실행하지 못했습니다.' }
        # 글자만 지우기(정밀): text_eraser.py가 이 가상환경의 OpenCV를 씁니다.
        $cv = Get-FirstLine $VenvPython @('-c', 'import cv2; print(cv2.__version__)')
        if ($cv) { Add-Result '글자 지우기(OpenCV)' 'OK' "OpenCV $cv" }
        else { Add-Result '글자 지우기(OpenCV)' '경고' '아직 없습니다. -CheckOnly 없이 이 스크립트를 실행하면 설치됩니다.' }
    }
    elseif (-not $CheckOnly) { Add-Result '미디어 가상환경' '실패' 'Python이 없어 가상환경을 만들지 못했습니다.' }

    # Chromium: CHROMIUM_PATH가 있으면 그 브라우저, 없으면 Playwright 전용 Chromium을 사용합니다.
    if (Test-Path -LiteralPath $VenvPython) {
        if ($env:CHROMIUM_PATH -and -not (Test-Path -LiteralPath $env:CHROMIUM_PATH -PathType Leaf)) {
            Add-Result 'Chromium' '실패' "CHROMIUM_PATH 경로에 파일이 없습니다: $($env:CHROMIUM_PATH)"
        }
        else {
            if (-not $env:CHROMIUM_PATH -and -not $CheckOnly) { & $VenvPython -m playwright install chromium }
            $launch = @'
import os, sys
from playwright.sync_api import sync_playwright
options = {'headless': True}
if os.environ.get('CHROMIUM_PATH'): options['executable_path'] = os.environ['CHROMIUM_PATH']
with sync_playwright() as p:
    browser = p.chromium.launch(**options)
    print(browser.version)
    browser.close()
'@
            $chromeOut = @(Invoke-PythonCode $VenvPython @() $launch @())
            $chromeVersion = $null
            if ($LASTEXITCODE -eq 0 -and $chromeOut.Count -gt 0) { $chromeVersion = ([string]$chromeOut[-1]).Trim() }
            if ($chromeVersion) { Add-Result 'Chromium' 'OK' "실행 확인 $chromeVersion" }
            elseif ($CheckOnly) { Add-Result 'Chromium' '경고' 'Playwright Chromium이 아직 없거나 실행되지 않습니다.' }
            else { Add-Result 'Chromium' '실패' 'Chromium 실행에 실패했습니다. 위 오류를 확인하세요.' }
        }
    }
    else { Add-Result 'Chromium' '건너뜀' '미디어 가상환경이 없어 확인하지 못했습니다.' }
}

# 6. 실제 렌더링 확인 -----------------------------------------------------
Write-Step '한글 제목 렌더링 시험 (앱과 같은 ASS 자막·글꼴 사용)'
if ($Python -and $ffmpeg) {
    $renderTest = @'
import pathlib, shutil, subprocess, sys, tempfile
sys.path.insert(0, sys.argv[1])
import production
# 앱의 render()와 같은 방식: ASS 자막 파일 + libass(fontsdir)로 한글 제목·자막을 그립니다.
font = pathlib.Path(production.FONT)
print('font=' + str(font))
if not font.is_file(): sys.exit('기본 글꼴 파일이 없습니다: ' + str(font))
print('fonts_folder=%d' % len(production.available_fonts()))
with tempfile.TemporaryDirectory() as d:
    work = pathlib.Path(d); fonts = work / 'fonts'; fonts.mkdir()
    shutil.copyfile(font, fonts / font.name)
    doc = {'headline': '한글 제목 테스트 123', 'script': '자막 확인 문장입니다.'}
    style = production.STYLES[next(iter(production.STYLES))]
    ass, _ = production.subtitles(doc, style, 1, True)
    (work / 'overlay.ass').write_text(ass, encoding='utf-8')
    out = work / 'check.mp4'
    size = '%dx%d' % (production.WIDTH, production.HEIGHT)
    vf = 'ass=filename=%s:fontsdir=%s' % (production.filter_path(work / 'overlay.ass'), production.filter_path(fonts))
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', '-f', 'lavfi', '-i', 'color=c=black:s=' + size + ':d=1', '-vf', vf, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(out)], check=True, timeout=60)
    probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'csv=p=0', str(out)], capture_output=True, text=True, check=True, timeout=20)
    print('render=' + probe.stdout.strip())
'@
    $renderOut = @(Invoke-PythonCode $Python.Path @() $renderTest @($ProjectRoot))
    if ($LASTEXITCODE -eq 0) { Add-Result '렌더링 시험' 'OK' (($renderOut | ForEach-Object { "$_".Trim() }) -join ', ') }
    else { Add-Result '렌더링 시험' '실패' '한글 제목 렌더링에 실패했습니다. FFmpeg에 libass(ass 필터)가 있는지와 위 오류를 확인하세요.' }
}
else { Add-Result '렌더링 시험' '건너뜀' 'Python 또는 FFmpeg가 없어 실행하지 않았습니다.' }

# 요약 --------------------------------------------------------------------
Write-Step '요약'
$Results | Format-Table -AutoSize -Wrap | Out-String -Width 200 | Write-Host
$failed = @($Results | Where-Object { $_.상태 -eq '실패' })
$warned = @($Results | Where-Object { $_.상태 -eq '경고' })
if ($failed.Count -gt 0) {
    Write-Host "실패 $($failed.Count)개. 위 안내대로 해결한 뒤 다시 실행하세요." -ForegroundColor Red
    exit 1
}
if ($warned.Count -gt 0) { Write-Host "경고 $($warned.Count)개가 있습니다." -ForegroundColor Yellow }
else { Write-Host '모두 준비됐습니다.' -ForegroundColor Green }
Write-Host '새로 설치한 프로그램이 있으면 새 cmd 창을 연 뒤 실행하세요:  set PORT=8020  그다음  python app.py'
exit 0
