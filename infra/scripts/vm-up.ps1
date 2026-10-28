<#
.SYNOPSIS
Starts the cfd-agent VM and prints the SSH command.

.PARAMETER UpdateIp
Point the NSG SSH rule at the current public IPv4 address first (use after your IP changed).
#>
[CmdletBinding()]
param(
    [switch]$UpdateIp,
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

function Assert-SingleIPv4([string]$Value) {
    # A single IPv4 address only: no CIDR, no wildcard, no service tag such as Internet.
    $parsed = $null
    if (-not [System.Net.IPAddress]::TryParse($Value, [ref]$parsed) -or
        $parsed.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
        $parsed.ToString() -ne $Value -or
        $Value -eq '0.0.0.0') {
        throw "Not a single public IPv4 address: '$Value'. SSH may only be opened to one address."
    }
}

if ($UpdateIp) {
    $ip = (Invoke-RestMethod -Uri 'https://api.ipify.org').Trim()
    Assert-SingleIPv4 $ip
    & $az network nsg rule update --resource-group $ResourceGroup --nsg-name nsg-cfdagent `
        --name Allow-SSH-Owner --source-address-prefixes "$ip/32" --output none
    if ($LASTEXITCODE -ne 0) { throw 'Updating the NSG rule failed.' }
    Write-Host "SSH rule now allows $ip"
}

& $az vm start --resource-group $ResourceGroup --name $VmName
if ($LASTEXITCODE -ne 0) { throw 'Starting the VM failed.' }

$vmIp = (& $az network public-ip show --resource-group $ResourceGroup --name pip-cfdagent `
        --query ipAddress --output tsv).Trim()
Write-Host 'VM is running. Connect with:'
Write-Host '  ssh cfdagent        (after copying scripts/ssh-config.example into ~/.ssh/config)'
Write-Host "  ssh -i ~/.ssh/id_cfdagent $AdminUser@$vmIp"
