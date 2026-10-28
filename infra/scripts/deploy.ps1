<#
.SYNOPSIS
Deploys (or previews) the cfd-agent infrastructure with one subscription-scope deployment.

.DESCRIPTION
Runs `az deployment sub create` with infra/main.bicep and infra/main.bicepparam. Creating Azure
resources needs the owner's explicit approval first; the script asks for a typed confirmation.

Environment variables read by main.bicepparam (documented in infra/README.md):
  CFDAGENT_BUDGET_EMAIL      required, budget alert address (never committed)
  CFDAGENT_OWNER_IP          optional, defaults to the current public IPv4 address
  CFDAGENT_SSH_PUBLIC_KEY    optional, defaults to the contents of ~/.ssh/id_cfdagent.pub
  CFDAGENT_OWNER_OBJECT_ID   optional, defaults to the signed-in Entra user's object id

.PARAMETER WhatIfOnly
Run `az deployment sub what-if` instead of creating anything.

.PARAMETER Force
Skip the typed confirmation.
#>
[CmdletBinding()]
param(
    [switch]$WhatIfOnly,
    [switch]$Force,
    [string]$Location = 'italynorth'
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

$infra = Split-Path -Parent $PSScriptRoot
$template = Join-Path $infra 'main.bicep'
$parameters = Join-Path $infra 'main.bicepparam'

if (-not $env:CFDAGENT_BUDGET_EMAIL) {
    throw 'Set CFDAGENT_BUDGET_EMAIL (budget alert address) before running this script.'
}
if (-not $env:CFDAGENT_OWNER_IP) {
    $env:CFDAGENT_OWNER_IP = (Invoke-RestMethod -Uri 'https://api.ipify.org').Trim()
}
Assert-SingleIPv4 $env:CFDAGENT_OWNER_IP
if (-not $env:CFDAGENT_SSH_PUBLIC_KEY) {
    $keyFile = Join-Path $HOME '.ssh\id_cfdagent.pub'
    $env:CFDAGENT_SSH_PUBLIC_KEY = (Get-Content -Raw $keyFile).Trim()
}
if (-not $env:CFDAGENT_OWNER_OBJECT_ID) {
    $env:CFDAGENT_OWNER_OBJECT_ID = (& $az ad signed-in-user show --query id -o tsv).Trim()
}

if ($WhatIfOnly) {
    & $az deployment sub what-if --location $Location --name cfdagent --template-file $template --parameters $parameters
    exit $LASTEXITCODE
}

if (-not $Force) {
    Write-Host 'This creates Azure resources (resource group rg-cfdagent, VM, storage, AI account, budget).'
    $answer = Read-Host 'Type "deploy" to continue, after the owner approved the what-if and the cost estimate'
    if ($answer -ne 'deploy') { throw 'Cancelled.' }
}

& $az deployment sub create --location $Location --name cfdagent --template-file $template --parameters $parameters
exit $LASTEXITCODE
