param(
    [string]$Root = (Join-Path $env:LOCALAPPDATA 'Programs\MamidLabs'),
    [switch]$Warmup
)

$ErrorActionPreference = 'Stop'
$python = Join-Path $Root 'venv\ocr\Scripts\python.exe'
$cpuPython = Join-Path $Root 'venv\ocr-cpu\Scripts\python.exe'
$worker = Join-Path $Root 'agent\ocr-tools.py'
$cache = Join-Path $Root 'tools\paddle-cache'
# The pip cache is written to the install root, not the user profile; it goes away with the root on uninstall.
$env:PIP_CACHE_DIR = Join-Path $Root 'tools\pip-cache'
$paddleWheel = 'https://paddle-qa.bj.bcebos.com/paddle-pipeline/Develop-TagBuild-Training-Windows-Gpu-Cuda12.9-Cudnn9.9-Trt10.5-Mkl-Avx-VS2019-SelfBuiltPypiUse/86d658f56ebf3a5a7b2b33ace48f22d10680d311/paddlepaddle_gpu-3.0.0.dev20250717-cp312-cp312-win_amd64.whl'
# Official stable PaddlePaddle GPU channel (paddlepaddle.org.cn); the special nightly/QA
# build above (paddle-qa.bj.bcebos.com) is not a permanent release channel - a fallback in
# case it is ever removed. Note: this channel may not carry the special sm_120 fix for the
# RTX 50 series (see the comment below), so the special build is tried first and this is
# only used if that one fails.
$paddleFallbackIndex = 'https://www.paddlepaddle.org.cn/packages/stable/cu126/'

try {
    New-Item -ItemType Directory -Path (Split-Path -Parent $python), $cache -Force | Out-Null
    if (!(Test-Path -LiteralPath $python -PathType Leaf)) {
        $basePython = Get-Command python.exe -ErrorAction SilentlyContinue
        if (!$basePython) { throw 'python.exe not found. Install Python 3 and run the installation again.' }
        & $basePython.Source -m venv (Split-Path -Parent (Split-Path -Parent $python))
    }

    & $python -m pip install --upgrade pip
    # Plain image OCR runs on the GPU. PDF OCR is pinned to the CPU separately inside the worker.
    # For the RTX 50 series on Windows use Paddle's special sm_120 wheel.
    # The standard cu126 wheel sees CUDA but the OCR detection kernel may return empty
    # results. The special wheel pins the CUDA 12.9 and cuDNN 9.9 runtimes.
    & $python -m pip install --upgrade --force-reinstall $paddleWheel
    if ($LASTEXITCODE -ne 0) {
        Write-Warning 'The special PaddlePaddle GPU build (nightly/QA channel) could not be downloaded; falling back to the official stable channel (cu126). That channel may not carry the fix for the empty-OCR-result issue on the RTX 50 series.'
        & $python -m pip install --upgrade --force-reinstall paddlepaddle-gpu -i $paddleFallbackIndex
        if ($LASTEXITCODE -ne 0) { throw 'PaddlePaddle GPU could not be installed (both the special build and the official stable channel failed).' }
    }
    & $python -m pip install --upgrade paddleocr pymupdf
    if ($LASTEXITCODE -ne 0) { throw 'paddleocr/pymupdf could not be installed.' }
    $basePython = Get-Command python.exe -ErrorAction SilentlyContinue
    if (!$basePython) { throw 'python.exe not found. Install Python 3 and run the installation again.' }
    if (!(Test-Path -LiteralPath $cpuPython -PathType Leaf)) {
        & $basePython.Source -m venv (Split-Path -Parent (Split-Path -Parent $cpuPython))
    }
    & $cpuPython -m pip install --upgrade pip
    & $cpuPython -m pip install --upgrade paddlepaddle paddleocr pymupdf
    if ($LASTEXITCODE -ne 0) { throw 'The PDF (CPU) OCR environment could not be installed.' }
    if (!(Test-Path -LiteralPath $worker -PathType Leaf)) {
        throw "OCR worker not found: $worker"
    }
    if ($Warmup) {
        $env:PADDLE_HOME = $cache
        $env:PADDLEOCR_HOME = $cache
        $env:PADDLEX_HOME = $cache
        $env:PADDLE_PDX_HOME = $cache
        '{"action":"warmup","device":"gpu"}' | & $python $worker
        '{"action":"warmup","device":"cpu"}' | & $cpuPython $worker
    }
    Write-Output "OCR installation complete. Environment: $python"
    Write-Output "PDF CPU environment: $cpuPython"
    Write-Output "Model cache: $cache"
} catch {
    Write-Error "OCR installation failed: $($_.Exception.Message)"
    exit 1
}
