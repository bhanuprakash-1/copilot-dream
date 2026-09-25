#Requires -Version 5.1
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Config,
  [string]$Model
)
$ErrorActionPreference = 'Stop'
$policy = (Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json).model_policy
if (-not $policy) { throw "Missing model_policy in $Config" }
$allowed = @($policy.allowed)
if (-not $allowed.Count -or $allowed.Where({ $_ -notmatch '^[a-z0-9][a-z0-9._-]*$' -or $_ -eq 'auto' }).Count) {
  throw 'model_policy.allowed must contain explicit model IDs, never auto.'
}
if ([string]::IsNullOrWhiteSpace($policy.default) -or $policy.default -notin $allowed) {
  throw 'model_policy.default must be in model_policy.allowed.'
}
if ($policy.context -ne 'long_context') { throw 'Dream requires long_context for every agent.' }
if ($policy.effort -ne 'xhigh') { throw 'Dream requires xhigh reasoning for every agent.' }
if ([string]::IsNullOrWhiteSpace($Model)) { $Model = [string]$policy.default }
if ($Model -notin $allowed) { throw "Model '$Model' is not allowed by $Config; no fallback is permitted." }
[pscustomobject]@{
  Model = $Model
  Context = [string]$policy.context
  Effort = [string]$policy.effort
}
