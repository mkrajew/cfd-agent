using 'main.bicep'

// Nothing personal is committed. Set these environment variables before deploying
// (see infra/README.md): scripts/deploy.ps1 fills in the first three automatically.
param ownerIp = readEnvironmentVariable('CFDAGENT_OWNER_IP')
param sshPublicKey = readEnvironmentVariable('CFDAGENT_SSH_PUBLIC_KEY')
param ownerObjectId = readEnvironmentVariable('CFDAGENT_OWNER_OBJECT_ID')
param budgetEmail = readEnvironmentVariable('CFDAGENT_BUDGET_EMAIL')
