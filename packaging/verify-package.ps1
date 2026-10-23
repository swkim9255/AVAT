# AVAT 배포 폴더 무결성 확인 (Windows PowerShell 5.1 이상)
#
# 반입한 AVAT 폴더에서 실행합니다:
#   powershell -ExecutionPolicy Bypass -File .\verify-package.ps1
#
# 1) SHA256SUMS.txt에 기록된 프로그램 파일이 모두 있고 내용이 같은지 확인합니다.
# 2) runtime\models\grounding-dino-base\의 모델 7개 파일이 있는지 확인하고,
#    MODEL-SHA256SUMS.txt가 있으면 빌드 검증에 쓴 모델과 같은 파일인지도 비교합니다.

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Test-Checksums([string]$listPath, [string]$baseDir) {
    $bad = 0
    $count = 0
    foreach ($line in [System.IO.File]::ReadAllLines($listPath)) {
        if (-not $line.Trim()) { continue }
        $expected, $relative = $line -split '  ', 2
        $path = Join-Path $baseDir ($relative -replace '/', '\')
        $count++
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            Write-Host "  없음: $relative" -ForegroundColor Red
            $bad++
            continue
        }
        $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $expected) {
            Write-Host "  불일치: $relative" -ForegroundColor Red
            $bad++
        }
    }
    return @($count, $bad)
}

Write-Host "프로그램 파일 확인 중... (수 분 걸릴 수 있습니다)"
$result = Test-Checksums (Join-Path $root 'SHA256SUMS.txt') $root
$failed = $result[1] -gt 0
if ($failed) {
    Write-Host ("프로그램 파일 {0}개 중 {1}개가 없거나 손상되었습니다. 배포본을 다시 복사하세요." -f $result[0], $result[1]) -ForegroundColor Red
} else {
    Write-Host ("프로그램 파일 {0}개 정상" -f $result[0]) -ForegroundColor Green
}

$modelDir = Join-Path $root 'runtime\models\grounding-dino-base'
$modelFiles = @('config.json', 'model.safetensors', 'preprocessor_config.json',
    'special_tokens_map.json', 'tokenizer.json', 'tokenizer_config.json', 'vocab.txt')
$missing = @($modelFiles | Where-Object { -not (Test-Path -LiteralPath (Join-Path $modelDir $_) -PathType Leaf) })
if ($missing.Count -gt 0) {
    Write-Host ("모델 파일이 없습니다: {0}" -f ($missing -join ', ')) -ForegroundColor Red
    Write-Host "  runtime\models\grounding-dino-base\ 폴더에 7개 파일을 복사해야 객체 검출을 쓸 수 있습니다."
    $failed = $true
} else {
    $modelSums = Join-Path $modelDir 'MODEL-SHA256SUMS.txt'
    if (Test-Path -LiteralPath $modelSums) {
        Write-Host "모델 파일 확인 중..."
        $modelResult = Test-Checksums $modelSums $modelDir
        if ($modelResult[1] -gt 0) {
            Write-Host "모델 파일이 빌드 검증에 쓴 파일과 다릅니다. 손상되었거나 다른 버전일 수 있으니 원본과 비교하세요." -ForegroundColor Yellow
            $failed = $true
        } else {
            Write-Host "모델 파일 7개 정상 (빌드 검증 모델과 동일)" -ForegroundColor Green
        }
    } else {
        Write-Host "모델 파일 7개 있음" -ForegroundColor Green
    }
}

if ($failed) { exit 1 }
Write-Host "확인 완료: AVAT.exe를 실행할 수 있습니다." -ForegroundColor Green
exit 0
