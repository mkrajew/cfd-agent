// cfd-agent infrastructure: one subscription-scope deployment.
// Creates the resource group and every resource in it, plus the subscription budget.
// Deployment happens only after the owner approves the what-if and the cost estimate.
targetScope = 'subscription'

@description('Region of the resource group and the AI Services account (Foundry models are verified in italynorth).')
@allowed([
  'italynorth'
  'spaincentral'
  'belgiumcentral'
  'denmarkeast'
  'austriaeast'
])
param location string = 'italynorth'

@description('''Region of the VM, its network and the storage account. No 4-vCPU size with quota is available to this
subscription in italynorth (NotAvailableForSubscription or quota 0, checked 2026-10-28); owner decision 2026-10-28:
belgiumcentral with Standard_D4as_v4.''')
@allowed([
  'italynorth'
  'spaincentral'
  'belgiumcentral'
  'denmarkeast'
  'austriaeast'
])
param vmLocation string = 'belgiumcentral'

@description('Resource group that holds everything except the budget.')
param resourceGroupName string = 'rg-cfdagent'

@description('SSH public key of the VM administrator. Passwords are disabled.')
@minLength(20)
param sshPublicKey string

@description('Entra object id of the owner; receives data-reader roles for Portal browsing.')
@minLength(36)
@maxLength(36)
param ownerObjectId string

@description('Budget alert e-mail address. Passed at deploy time, never committed.')
@minLength(5)
param budgetEmail string

@description('Daily auto-shutdown time, HHmm (local time of shutdownTimeZone).')
@minLength(4)
@maxLength(4)
param shutdownTime string = '2300'

@description('IANA time zone of the daily shutdown. Microsoft.DevTestLab/schedules (Windows zone ids) is not offered in any allowed region, so a systemd timer on the VM does the job.')
param shutdownTimeZone string = 'Europe/Warsaw'

@description('Linux administrator user name.')
param adminUsername string = 'azureuser'

@description('VM size. Student quota allows at most 4 vCPU per family. Standard_D4as_v4: 4 vCPU (AMD, 2 cores with SMT), 16 GB.')
param vmSize string = 'Standard_D4as_v4'

@description('OS disk size in GiB. 64 GiB StandardSSD (E6) leaves room for the image (about 10 GB unpacked) and runs.')
param osDiskSizeGB int = 64

@description('OS disk SKU. StandardSSD_LRS is the cheapest option with predictable latency.')
@allowed([
  'Standard_LRS'
  'StandardSSD_LRS'
  'Premium_LRS'
])
param osDiskSku string = 'StandardSSD_LRS'

@description('Container image that cloud-init pre-pulls.')
param containerImage string = 'ghcr.io/mkrajew/cfd-agent:latest'

// qwen3-32b is left out on purpose: ARM preflight rejects it on an AIServices account in italynorth
// ("SKU 'GlobalStandard' / 'DeveloperTier' of account deployment is not supported by the model
// 'qwen3-32b' version '1'", format 'Alibaba'), although `az cognitiveservices model list` offers both.
// Task 11 applies the fallback rule.
@description('Model deployments on the AI Services account. Capacity is in thousands of tokens per minute; billing is per token.')
param modelDeployments array = [
  {
    name: 'gpt-oss-120b'
    format: 'OpenAI-OSS'
    version: '1'
    sku: 'GlobalStandard'
    capacity: 50
  }
  {
    name: 'Llama-3.3-70B-Instruct'
    format: 'Meta'
    version: '9'
    sku: 'GlobalStandard'
    capacity: 20
  }
]

@description('Disable key authentication on the AI account (Entra ID only). Turn on after Entra access is verified for every deployment.')
param disableLocalAuth bool = false

@description('Monthly budget in USD.')
param budgetAmount int = 100

@description('Start of the budget: the first day of the current month, computed at deployment time (a past start outside the current month is rejected by the Consumption API).')
param budgetStartDate string = '${utcNow('yyyy-MM')}-01T00:00:00Z'

var tags = {
  project: 'cfd-agent'
}

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module network 'modules/network.bicep' = {
  name: 'network'
  scope: rg
  params: {
    location: vmLocation
    tags: tags
  }
}

module storage 'modules/storage.bicep' = {
  name: 'storage'
  scope: rg
  params: {
    location: vmLocation
    tags: tags
  }
}

module foundry 'modules/foundry.bicep' = {
  name: 'foundry'
  scope: rg
  params: {
    location: location
    tags: tags
    modelDeployments: modelDeployments
    disableLocalAuth: disableLocalAuth
  }
}

module vm 'modules/vm.bicep' = {
  name: 'vm'
  scope: rg
  params: {
    location: vmLocation
    tags: tags
    vmSize: vmSize
    adminUsername: adminUsername
    sshPublicKey: sshPublicKey
    osDiskSizeGB: osDiskSizeGB
    osDiskSku: osDiskSku
    subnetId: network.outputs.subnetId
    publicIpId: network.outputs.publicIpId
    shutdownTime: shutdownTime
    shutdownTimeZone: shutdownTimeZone
    containerImage: containerImage
  }
}

module roles 'modules/roles.bicep' = {
  name: 'roles'
  scope: rg
  params: {
    vmName: vm.outputs.vmName
    vmPrincipalId: vm.outputs.principalId
    storageAccountName: storage.outputs.storageAccountName
    aiAccountName: foundry.outputs.aiAccountName
    ownerObjectId: ownerObjectId
  }
}

module budget 'modules/budget.bicep' = {
  name: 'budget'
  params: {
    budgetAmount: budgetAmount
    budgetEmail: budgetEmail
    startDate: budgetStartDate
  }
}

output resourceGroupName string = rg.name
output vmName string = vm.outputs.vmName
output vmPublicIp string = network.outputs.publicIpAddress
output aiEndpoint string = foundry.outputs.aiEndpoint
output storageAccountName string = storage.outputs.storageAccountName
