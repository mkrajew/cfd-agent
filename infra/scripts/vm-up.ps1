<#
.SYNOPSIS
Starts the cfd-agent VM and prints the SSH command.
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup = 'rg-cfdagent',
    [string]$VmName = 'vm-cfdagent',
    [string]$AdminUser = 'azureuser'
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

& $az vm start --resource-group $ResourceGroup --name $VmName
if ($LASTEXITCODE -ne 0) { throw 'Starting the VM failed.' }

$vmIp = (& $az network public-ip show --resource-group $ResourceGroup --name pip-cfdagent `
        --query ipAddress --output tsv).Trim()
Write-Host 'VM is running. Connect with:'
Write-Host '  ssh cfdagent        (after copying scripts/ssh-config.example into ~/.ssh/config)'
Write-Host "  ssh -i ~/.ssh/id_cfdagent $AdminUser@$vmIp"
