<#
.SYNOPSIS
Deallocates the cfd-agent VM (compute billing stops; disk and public IP keep billing).
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup = 'rg-cfdagent',
    [string]$VmName = 'vm-cfdagent'
)

$ErrorActionPreference = 'Stop'

function Get-Az {
    $cmd = Get-Command az -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $default = 'C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd'
    if (Test-Path $default) { return $default }
    throw 'Azure CLI not found. Install it or add it to PATH.'
}
$az = Get-Az

& $az vm deallocate --resource-group $ResourceGroup --name $VmName
if ($LASTEXITCODE -ne 0) { throw 'Deallocating the VM failed.' }

$state = & $az vm get-instance-view --resource-group $ResourceGroup --name $VmName `
    --query "instanceView.statuses[?starts_with(code, 'PowerState/')].displayStatus | [0]" --output tsv
Write-Host "Power state: $state"
