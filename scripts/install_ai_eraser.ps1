# mflowshorts AI 자막 지우기 설치 (NVIDIA 그래픽카드 전용)
#
# 실행 (프로젝트 폴더에서):
#   scripts\install_ai_eraser.cmd                 그래픽카드에 맞는 패키지를 골라 내려받고 data\vsr 에 풉니다.
#   scripts\install_ai_eraser.cmd -CheckOnly      아무것도 받지 않고 그래픽카드와 설치 상태만 확인
#   scripts\install_ai_eraser.cmd -Archive D:\다운로드\vsr-windows-nvidia-cuda-12.8.7z
#                                                 직접 내려받은 압축 파일을 data\vsr 에 풉니다.
#
# 쓰는 도구: video-subtitle-remover (YaoFANGUK, Apache-2.0) Windows 실행 패키지.
# 패키지 안에 Python·PyTorch·PaddleOCR이 들어 있어 PC의 Python은 바꾸지 않습니다. 용량이 수 GB입니다.
# 기존 data\vsr 가 있으면 지우지 않고 점검만 합니다.
[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [string]$Archive = ''
)

$ErrorActionPreference = 'Continue'
Set-StrictMode -Version 2.0
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Pointer = Join-Path $ProjectRoot 'data\vsr-path.txt'

function Test-Ascii([string]$Path) { return ($Path -notmatch '[^\x20-\x7E]') }
# 이 도구의 문자인식(PaddleOCR)은 Windows에서 한글이 들어간 폴더의 모델을 읽지 못합니다.
# 그래서 영문 경로에 설치하고, 위치를 data\vsr-path.txt 에 적어 앱이 찾게 합니다.
if (Test-Path -LiteralPath $Pointer) { $Target = (Get-Content -LiteralPath $Pointer -Encoding UTF8 -TotalCount 1).Trim() }
elseif (Test-Ascii (Join-Path $ProjectRoot 'data\vsr')) { $Target = Join-Path $ProjectRoot 'data\vsr' }
elseif ($env:LOCALAPPDATA -and (Test-Ascii $env:LOCALAPPDATA)) { $Target = Join-Path $env:LOCALAPPDATA 'mflowshorts\vsr' }
else { $Target = Join-Path $env:SystemDrive 'mflowshorts-vsr' }
$Downloads = Join-Path $Target '_download'
$Repo = 'YaoFANGUK/video-subtitle-remover'
$ReleasePage = "https://github.com/$Repo/releases"

function Write-Step([string]$Text) { Write-Host ''; Write-Host "== $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "[OK] $Text" -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host "[경고] $Text" -ForegroundColor Yellow }
function Stop-WithError([string]$Text) { Write-Host "[실패] $Text" -ForegroundColor Red; exit 1 }

function Find-Package {
    # 앱(production.ai_eraser)과 같은 규칙: data\vsr 아래 3단계 안의 backend\main.py 와 그 옆 python.exe
    if (-not (Test-Path -LiteralPath $Target)) { return $null }
    $mains = @(Get-ChildItem -LiteralPath $Target -Recurse -Depth 3 -Filter 'main.py' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Directory.Name -eq 'backend' })
    foreach ($main in $mains) {
        $root = $main.Directory.Parent.FullName
        $candidates = @('python\python.exe', 'python.exe', 'runtime\python.exe', '.venv\Scripts\python.exe', 'venv\Scripts\python.exe') |
            ForEach-Object { Join-Path $root $_ }
        $candidates += @(Get-ChildItem -LiteralPath $root -Filter 'python.exe' -Depth 1 -File -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
        $candidates += @(Get-ChildItem -LiteralPath $Target -Filter 'python.exe' -Depth 1 -File -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
        foreach ($python in $candidates) {
            if (Test-Path -LiteralPath $python) { return [pscustomobject]@{ Root = $root; Python = $python } }
        }
    }
    return $null
}

function Test-Package($Package) {
    Write-Step 'AI 도구 실행 점검 (처음에는 1~2분 걸릴 수 있음)'
    $check = Join-Path ([IO.Path]::GetTempPath()) 'mflow_vsr_check.py'
    $code = "import torch`nprint('cuda=' + str(torch.cuda.is_available()))`nprint('gpu=' + (torch.cuda.get_device_name(0) if torch.cuda.is_available() else '-'))`n"
    [IO.File]::WriteAllText($check, $code, (New-Object Text.UTF8Encoding $false))
    Push-Location -LiteralPath $Package.Root
    try { $out = @(& $Package.Python $check 2>$null) } finally { Pop-Location; Remove-Item -LiteralPath $check -Force -ErrorAction SilentlyContinue }
    $text = ($out | ForEach-Object { "$_".Trim() }) -join ', '
    if ($LASTEXITCODE -ne 0) { Write-Warn "패키지 Python이 PyTorch를 불러오지 못했습니다. 압축이 끝까지 풀렸는지 확인하세요. ($($Package.Python))"; return $false }
    if ($text -notmatch 'cuda=True') { Write-Warn "PyTorch는 있지만 그래픽카드(CUDA)를 쓰지 못합니다: $text. NVIDIA 드라이버를 최신으로 업데이트하세요. (CPU로는 매우 느립니다)"; return $true }
    Write-Ok "그래픽카드 사용 가능: $text"
    return $true
}

function Find-SevenZip {
    $paths = @((Get-Command 7z -ErrorAction SilentlyContinue | ForEach-Object { $_.Source }),
        (Join-Path $env:ProgramFiles '7-Zip\7z.exe'), (Join-Path ${env:ProgramFiles(x86)} '7-Zip\7z.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\7-Zip\7z.exe')) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
    return @($paths | Select-Object -First 1)[0]
}

function Expand-Package([string]$File) {
    Write-Step "압축 풀기 → $Target"
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
    $seven = Find-SevenZip
    if (-not $seven -and (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host 'winget으로 7-Zip 설치 중 (7z 압축을 풀기 위해 필요)...'
        & winget install --exact --id 7zip.7zip --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
        $seven = Find-SevenZip
    }
    if ($seven) {
        & $seven x $File "-o$Target" -y | Out-Null
        if ($LASTEXITCODE -eq 0) { return $true }
        Write-Warn "7-Zip 압축 풀기 실패 (코드 $LASTEXITCODE)"
    }
    # Windows 11 의 tar 는 7z 를 풀 수 있는 경우가 있습니다.
    & tar -xf $File -C $Target
    return ($LASTEXITCODE -eq 0)
}

function Get-Flavor {
    # 그래픽카드 세대(compute capability)와 드라이버 버전으로 CUDA 패키지를 고릅니다. (README 표 기준)
    if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) { return $null }
    $line = @(& nvidia-smi --query-gpu=name,compute_cap,driver_version --format=csv,noheader 2>$null) | Select-Object -First 1
    if ($LASTEXITCODE -ne 0 -or -not $line) {
        # 오래된 드라이버는 compute_cap 항목을 모릅니다. 세대는 흔한 값(7.5)으로 가정합니다.
        $line = @(& nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>$null) | Select-Object -First 1
        if ($LASTEXITCODE -ne 0 -or -not $line) { return $null }
        $p = "$line".Split(',') | ForEach-Object { $_.Trim() }
        $line = "$($p[0]), 7.5, $($p[1])"
    }
    $parts = "$line".Split(',') | ForEach-Object { $_.Trim() }
    $name = $parts[0]; $cap = 0.0; $driver = 0.0
    [void][double]::TryParse($parts[1], [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$cap)
    [void][double]::TryParse(($parts[2].Split('.')[0]), [ref]$driver)
    if ($cap -ge 9.0) { $flavor = 'cuda-12.8' }           # RTX 50 시리즈 등 최신 세대
    elseif ($driver -ge 570 -and $cap -ge 5.0) { $flavor = 'cuda-12.8' }
    elseif ($driver -ge 560 -and $cap -ge 5.0) { $flavor = 'cuda-12.6' }
    else { $flavor = 'cuda-11.8' }
    return [pscustomobject]@{ Name = $name; Cap = $parts[1]; CapValue = $cap; Driver = $parts[2]; DriverMajor = $driver; Flavor = $flavor }
}

function Find-Assets([string]$Flavor) {
    # 최근 릴리스에서 Windows NVIDIA 패키지(분할 압축 포함)를 찾습니다.
    try { $releases = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases?per_page=10" -Headers @{ 'User-Agent' = 'mflowshorts-installer' } -TimeoutSec 30 }
    catch { Write-Warn "릴리스 목록을 읽지 못했습니다: $($_.Exception.Message)"; return $null }
    foreach ($release in $releases) {
        $assets = @($release.assets | Where-Object { $_.name -match 'windows' -and $_.name -match 'nvidia' -and $_.name -match [regex]::Escape($Flavor) })
        if ($assets.Count -gt 0) { return [pscustomobject]@{ Tag = $release.tag_name; Assets = @($assets | Sort-Object name) } }
    }
    return $null
}

Write-Host "mflowshorts AI 자막 지우기 설치: $ProjectRoot"

Write-Step '그래픽카드'
$gpu = Get-Flavor
if (-not $gpu) { Stop-WithError 'nvidia-smi 가 응답하지 않습니다. NVIDIA 그래픽카드와 드라이버가 있어야 AI 자막 지우기를 쓸 수 있습니다.' }
Write-Ok "$($gpu.Name) (compute $($gpu.Cap), 드라이버 $($gpu.Driver)) → $($gpu.Flavor) 패키지"
if ($gpu.CapValue -ge 10 -and $gpu.DriverMajor -lt 570) { Write-Warn '이 그래픽카드는 드라이버 570 이상이 필요합니다. NVIDIA 앱이나 nvidia.com 에서 드라이버를 먼저 업데이트하세요.' }

$package = Find-Package
if ($package) {
    Write-Ok "이미 설치됨: $($package.Root)"
    if (-not $CheckOnly) { [IO.File]::WriteAllText($Pointer, $Target, (New-Object Text.UTF8Encoding $false)) }
    [void](Test-Package $package)
    Write-Host ''; Write-Host '앱을 다시 켜면 원본 글자 가리기에서 "AI 자막 지우기 (NVIDIA)"를 고를 수 있습니다.'
    exit 0
}
if ($CheckOnly) { Write-Warn "아직 설치되지 않았습니다. scripts\install_ai_eraser.cmd 를 옵션 없이 실행하세요."; exit 0 }

if ($Archive) {
    if (-not (Test-Path -LiteralPath $Archive)) { Stop-WithError "압축 파일이 없습니다: $Archive" }
    $first = (Resolve-Path -LiteralPath $Archive).Path
}
else {
    Write-Step "패키지 찾기 ($ReleasePage)"
    $found = Find-Assets $gpu.Flavor
    if (-not $found) {
        Write-Warn "자동으로 받을 $($gpu.Flavor) 패키지를 찾지 못했습니다."
        Write-Host "1) $ReleasePage 에서 vsr-windows-nvidia-$($gpu.Flavor) 이 들어간 파일(분할이면 전부)을 내려받으세요."
        Write-Host '2) 그다음:  scripts\install_ai_eraser.cmd -Archive "내려받은 파일 경로(.7z 또는 .7z.001)"'
        exit 1
    }
    $totalMb = [math]::Round((($found.Assets | Measure-Object -Property size -Sum).Sum) / 1MB)
    Write-Ok "$($found.Tag): $(($found.Assets | ForEach-Object { $_.name }) -join ', ') (약 $totalMb MB)"
    New-Item -ItemType Directory -Force -Path $Downloads | Out-Null
    foreach ($asset in $found.Assets) {
        $file = Join-Path $Downloads $asset.name
        if ((Test-Path -LiteralPath $file) -and (Get-Item -LiteralPath $file).Length -eq $asset.size) { Write-Ok "이미 받음: $($asset.name)"; continue }
        Write-Step "내려받는 중: $($asset.name) (끊기면 다시 실행하면 이어 받습니다)"
        & curl.exe -L --fail --retry 5 -C - -o $file $asset.browser_download_url
        if ($LASTEXITCODE -ne 0 -or (Get-Item -LiteralPath $file).Length -ne $asset.size) { Stop-WithError "$($asset.name) 내려받기 실패. 다시 실행하면 이어서 받습니다." }
    }
    $first = Join-Path $Downloads $found.Assets[0].name
}

if (-not (Expand-Package $first)) { Stop-WithError "압축을 풀지 못했습니다. 7-Zip(https://www.7-zip.org)을 설치한 뒤 다시 실행하세요." }
New-Item -ItemType Directory -Force -Path (Split-Path $Pointer) | Out-Null
[IO.File]::WriteAllText($Pointer, $Target, (New-Object Text.UTF8Encoding $false))
$package = Find-Package
if (-not $package) {
    Write-Host '풀린 폴더 맨 위:'; Get-ChildItem -LiteralPath $Target | Select-Object -First 20 | ForEach-Object { Write-Host "  $($_.Name)" }
    Stop-WithError '패키지 안에서 backend\main.py 와 python.exe 를 찾지 못했습니다. 위 폴더 목록을 Claude에게 보내 주세요.'
}
Write-Ok "설치됨: $($package.Root)"
[void](Test-Package $package)
Write-Host ''
Write-Host '앱을 다시 켜면 원본 글자 가리기에서 "AI 자막 지우기 (NVIDIA)"를 고를 수 있습니다.' -ForegroundColor Green
Write-Host "내려받은 압축 파일은 $Downloads 에 남아 있습니다. 설치가 잘 되면 지워도 됩니다."
exit 0
