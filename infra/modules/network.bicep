// VNet, NSG and a static public IP. SSH (22) is open to the Internet (owner decision 2026-10-29: the
// owner connects from changing networks and a VPN); the VM allows key authentication only and runs
// fail2ban (infra/vm/cloud-init.yaml). Every other inbound port is denied by the NSG default rules.
param location string
param tags object

var vnetName = 'vnet-cfdagent'
var subnetName = 'snet-cfdagent'
var nsgName = 'nsg-cfdagent'
var publicIpName = 'pip-cfdagent'

resource nsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: nsgName
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'Allow-SSH-Internet'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'Internet'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '22'
        }
      }
    ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: vnetName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.20.0.0/16'
      ]
    }
    subnets: [
      {
        name: subnetName
        properties: {
          addressPrefix: '10.20.0.0/24'
          networkSecurityGroup: {
            id: nsg.id
          }
        }
      }
    ]
  }
}

// Standard SKU is the only SKU still offered. A static Standard IPv4 address is billed
// hourly even while the VM is deallocated.
resource publicIp 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: publicIpName
  location: location
  tags: tags
  sku: {
    name: 'Standard'
    tier: 'Regional'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

output subnetId string = '${vnet.id}/subnets/${subnetName}'
output publicIpId string = publicIp.id
output publicIpAddress string = publicIp.properties.ipAddress
