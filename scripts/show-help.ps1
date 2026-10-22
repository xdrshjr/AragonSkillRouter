# Report the help metadata of a script, so the documented flags can be checked.
param([Parameter(Mandatory = $true)][string]$Path)

$help = Get-Help $Path -Detailed
Write-Host ('Synopsis: ' + $help.Synopsis)
Write-Host ('Examples: ' + @($help.examples.example).Count)
Write-Host 'Parameters documented:'
foreach ($parameter in $help.parameters.parameter) {
    Write-Host ('  -' + $parameter.name)
}
