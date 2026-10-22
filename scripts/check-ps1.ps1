# Parse-check a PowerShell file without executing it.
#   powershell -NoProfile -File scripts\check-ps1.ps1 -Path build-desktop.ps1
param([Parameter(Mandatory = $true)][string[]]$Path)

$failed = 0
foreach ($file in $Path) {
    if (-not (Test-Path $file)) {
        Write-Host "MISSING $file" -ForegroundColor Red
        $failed++
        continue
    }
    $tokens = $null
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile(
        (Resolve-Path $file).Path, [ref]$tokens, [ref]$errors)
    if ($errors -and $errors.Count -gt 0) {
        Write-Host "SYNTAX ERRORS in $file" -ForegroundColor Red
        foreach ($error in $errors) {
            Write-Host ("  line {0}: {1}" -f $error.Extent.StartLineNumber, $error.Message) -ForegroundColor Red
        }
        $failed++
    } else {
        Write-Host ("PARSE OK {0} ({1} lines)" -f $file, (Get-Content $file).Count) -ForegroundColor Green
    }
}

if ($failed -gt 0) { exit 1 }
exit 0
