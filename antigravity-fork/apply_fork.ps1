$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
python .\fork_toolbar.py --apply @args
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
