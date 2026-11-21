<#
.SYNOPSIS
  Opens, shows and closes iSCSI sessions with several MC/S connections on the Windows initiator,
  without the iscsicpl dialog.

.DESCRIPTION
  Uses the Microsoft iSCSI initiator (its WMI interface in root/wmi and the iSCSI cmdlets). Run it
  in an elevated PowerShell.

  -Target and -Portal: without a session to the target it logs in; with one it adds or removes
  connections until the wanted number is reached, then sets the MCS load balance policy. A new
  session starts with Round Robin, so the policy is always set explicitly. Without -Connections
  it opens as many connections as the target allows (max_connections in its YAML).

  -List: lists the targets of a portal with the maximum number of connections per session. The
  maximum is only known after a login: for a target without a session the script logs in briefly
  without reporting its disks to Windows, reads the value and logs out again.

  CHAP: with -ChapUser the login and every further MC/S connection authenticate with CHAP; each
  connection of a session logs in on its own and needs the secret again. Set the same user and
  secret on the target with "iscsictl chap set". The secret is asked for without echo unless
  -ChapSecret is given.

.PARAMETER Target
  Name (IQN) of the target.

.PARAMETER Portal
  IPv4 or IPv6 address of the target portal.

.PARAMETER Port
  TCP port of the portal. Default 3260.

.PARAMETER Connections
  Number of connections the session shall have. Default: the maximum the target allows.

.PARAMETER Policy
  MCS load balance policy: LeastQueueDepth (default), RoundRobin or FailOverOnly.

.PARAMETER Reconnect
  Log out every session to the target first, then log in again (new source ports, so the
  connections are spread anew). The target's disks disappear for a moment; close open files first.

.PARAMETER Show
  Only show the sessions to the target, their connections and policy.

.PARAMETER Disconnect
  Log out every session to the target. Close open files on its disks first.

.PARAMETER List
  List the targets of the portal with the maximum number of connections per session.

.PARAMETER ChapUser
  CHAP user (CHAP name) the initiator logs in with; switches CHAP on. The same as the <user> of
  "iscsictl chap set" on the target.

.PARAMETER ChapSecret
  CHAP secret as a SecureString (for scripts). Without it the secret is asked for. The Windows
  initiator accepts 12 to 16 characters.

.PARAMETER MutualChap
  Also let the target authenticate itself (mutual CHAP, "iscsictl chap set-mutual" on the target).
  The initiator needs its own secret for that, see -InitiatorSecret.

.PARAMETER InitiatorSecret
  Set the initiator's own CHAP secret (SecureString) for mutual CHAP: the secret of "iscsictl chap
  set-mutual". It is one setting for the whole Windows initiator, used for every target with
  mutual CHAP. Without it, a secret set before is used.

.EXAMPLE
  .\mcs-session.ps1 -Target iqn.2026-01.example.host:storage -Portal 192.0.2.10

.EXAMPLE
  .\mcs-session.ps1 -Target iqn.2026-01.example.host:storage -Portal 192.0.2.10 -Connections 8

.EXAMPLE
  .\mcs-session.ps1 -Target iqn.2026-01.example.host:storage -Show

.EXAMPLE
  .\mcs-session.ps1 -Target iqn.2026-01.example.host:storage -Disconnect

.EXAMPLE
  .\mcs-session.ps1 -Portal 192.0.2.10 -List

.EXAMPLE
  .\mcs-session.ps1 -Target iqn.2026-01.example.host:storage -Portal 192.0.2.10 -ChapUser client1

.EXAMPLE
  .\mcs-session.ps1 -Target iqn.2026-01.example.host:storage -Portal 192.0.2.10 -ChapUser client1 -MutualChap
#>
[CmdletBinding(DefaultParameterSetName = 'Set')]
param(
    [Parameter(Mandatory, ParameterSetName = 'Set')]
    [Parameter(Mandatory, ParameterSetName = 'Show')]
    [Parameter(Mandatory, ParameterSetName = 'Disconnect')]
    [string]$Target,

    [Parameter(Mandatory, ParameterSetName = 'Set')]
    [Parameter(Mandatory, ParameterSetName = 'List')]
    [string]$Portal,

    [Parameter(ParameterSetName = 'Set')]
    [Parameter(ParameterSetName = 'List')]
    [uint16]$Port = 3260,

    [Parameter(ParameterSetName = 'Set')]
    [ValidateRange(1, 65535)]
    [int]$Connections,

    [Parameter(ParameterSetName = 'Set')]
    [ValidateSet('LeastQueueDepth', 'RoundRobin', 'FailOverOnly')]
    [string]$Policy = 'LeastQueueDepth',

    [Parameter(ParameterSetName = 'Set')]
    [switch]$Reconnect,

    [Parameter(Mandatory, ParameterSetName = 'Show')]
    [switch]$Show,

    [Parameter(Mandatory, ParameterSetName = 'Disconnect')]
    [switch]$Disconnect,

    [Parameter(Mandatory, ParameterSetName = 'List')]
    [switch]$List,

    [Parameter(ParameterSetName = 'Set')]
    [Parameter(ParameterSetName = 'List')]
    [string]$ChapUser,

    [Parameter(ParameterSetName = 'Set')]
    [Parameter(ParameterSetName = 'List')]
    [securestring]$ChapSecret,

    [Parameter(ParameterSetName = 'Set')]
    [Parameter(ParameterSetName = 'List')]
    [switch]$MutualChap,

    [Parameter(ParameterSetName = 'Set')]
    [Parameter(ParameterSetName = 'List')]
    [securestring]$InitiatorSecret
)
$ErrorActionPreference = 'Stop'
$ns = 'root/wmi'
# ISCSI_Supported_LB_Policies.LoadBalancePolicy (ValueMap of the WMI schema)
$codes = @{ FailOverOnly = 1; RoundRobin = 2; LeastQueueDepth = 4 }
$names = @{ 1 = 'Fail Over Only'; 2 = 'Round Robin'; 3 = 'Round Robin with Subset'
    4 = 'Least Queue Depth'; 5 = 'Weighted Paths'; 6 = 'Vendor Specific' }
$anyInitiatorPort = [uint32]::MaxValue

# ISCSI_LOGIN_OPTIONS in iscsidsc.h (Windows SDK)
$loginOptionsVersion = [uint32]0
$infoUsername = 0x20
$infoPassword = 0x40
$infoAuthType = 0x80
$authChap = [uint32]1
$authMutualChap = [uint32]2

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).
    IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw 'Run this script in an elevated PowerShell.' }

function ConvertTo-Plain([securestring]$secure) {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function Assert-SecretLength([string]$secret, [string]$what) {
    if ($secret.Length -lt 12 -or $secret.Length -gt 16) {
        throw "The $what has $($secret.Length) characters; the Windows initiator accepts 12 to 16."
    }
}

if ($MutualChap -and -not $ChapUser) { throw '-MutualChap needs -ChapUser.' }
if ($InitiatorSecret -and -not $MutualChap) { throw '-InitiatorSecret is only used with -MutualChap.' }
$chap = $null
if ($ChapUser) {
    if (-not $ChapSecret) { $ChapSecret = Read-Host -AsSecureString "CHAP secret of $ChapUser (the one of iscsictl chap set)" }
    $chap = [pscustomobject]@{ User = $ChapUser; Secret = (ConvertTo-Plain $ChapSecret) }
    Assert-SecretLength $chap.Secret 'CHAP secret'
}
if ($InitiatorSecret) {
    $plain = ConvertTo-Plain $InitiatorSecret
    Assert-SecretLength $plain 'initiator secret'
    if ($plain -eq $chap.Secret) { throw 'The initiator secret must differ from the CHAP secret.' }
    Write-Host 'Setting the initiator CHAP secret (one setting for the whole initiator)'
    Set-IscsiChapSecret -ChapSecret $plain -ErrorAction Stop
}

# Parameters for Connect-IscsiTarget: CHAP if asked for.
function Get-AuthParameters {
    if (-not $chap) { return @{} }
    @{ AuthenticationType = $(if ($MutualChap) { 'MUTUALCHAP' } else { 'ONEWAYCHAP' })
        ChapUsername = $chap.User; ChapSecret = $chap.Secret }
}

# Login options for AddConnection: every MC/S connection authenticates on its own.
function New-LoginOptions {
    $p = @{ Version = $loginOptionsVersion; InformationSpecified = [uint32]0; LoginFlags = [uint32]0
        AuthType = [uint32]0; HeaderDigest = [uint32]0; DataDigest = [uint32]0
        MaximumConnections = [uint32]0; DefaultTime2Wait = [uint32]0; DefaultTime2Retain = [uint32]0 }
    if ($chap) {
        $p.InformationSpecified = [uint32]($infoUsername -bor $infoPassword -bor $infoAuthType)
        $p.AuthType = if ($MutualChap) { $authMutualChap } else { $authChap }
        # ANSI strings, as iscsidsc.h expects them.
        $p.Username = [byte[]][Text.Encoding]::Default.GetBytes($chap.User)
        $p.Password = [byte[]][Text.Encoding]::Default.GetBytes($chap.Secret)
    }
    New-CimInstance -Namespace $ns -ClassName MSiSCSIInitiator_TargetLoginOptions -ClientOnly -Property $p
}

# Callers wrap the result in @(): PowerShell unrolls a returned array, and a single CIM instance
# has no Count.
function Get-Sessions([string]$name) {
    Get-CimInstance -Namespace $ns -ClassName MSiSCSIInitiator_SessionClass |
        Where-Object TargetName -eq $name
}

# The initiator's CIM methods report success as $true or as 0.
function Assert-Ok($result, $what) {
    $ok = if ($result.ReturnValue -is [bool]) { $result.ReturnValue } else { $result.ReturnValue -eq 0 }
    if (-not $ok) { throw "$what returned $($result.ReturnValue)" }
}

# CID is two bytes, low byte first ({5, 0} is CID 5).
function Get-Cid($connection) {
    [int]$connection.CID[0] + 256 * [int]$connection.CID[1]
}

# The second half of a session id ("4000013700000001-4000013700000005") is the number the
# session info and load balance classes use.
function Get-IdNumber([string]$id) {
    [Convert]::ToUInt64(($id -split '-')[-1], 16)
}

# Maximum connections negotiated at the login of the session.
function Get-MaxConnections($session) {
    $want = Get-IdNumber $session.SessionId
    foreach ($info in @(Get-CimInstance -Namespace $ns -ClassName MSiSCSI_InitiatorSessionInfo)) {
        foreach ($s in @($info.SessionsList)) {
            if ($s.UniqueSessionId -eq $want) { return [int]$s.MaxConnections }
        }
    }
    $null
}

function Get-LbEntry($session) {
    $want = Get-IdNumber $session.SessionId
    foreach ($q in @(Get-CimInstance -Namespace $ns -ClassName MSiSCSI_QueryLBPolicy)) {
        foreach ($p in @($q.LoadBalancePolicies)) {
            if ($p.UniqueSessionId -eq $want) { return [pscustomobject]@{ Query = $q; Entry = $p } }
        }
    }
    $null
}

function Show-State {
    $sessions = @(Get-Sessions $Target)
    if (-not $sessions) { Write-Host "No session to $Target."; return }
    foreach ($s in $sessions) {
        $lb = Get-LbEntry $s
        $policy = if ($lb) { $names[[int]$lb.Entry.LoadBalancePolicy] } else { 'unknown' }
        Write-Host ('Session {0}: {1} of at most {2} connection(s), policy {3}' -f $s.SessionId,
            @($s.ConnectionInformation).Count, (Get-MaxConnections $s), $policy)
    }
}

function Remove-Sessions([string]$name) {
    foreach ($s in @(Get-Sessions $name)) {
        Write-Host "Logging out $($s.SessionId)"
        Assert-Ok (Invoke-CimMethod -InputObject $s -MethodName Logout) 'Logout'
    }
}

function Add-Portal {
    $known = Get-IscsiTargetPortal | Where-Object TargetPortalAddress -eq $Portal
    if ($known) {
        Update-IscsiTargetPortal -TargetPortalAddress $Portal -TargetPortalPortNumber $Port -ErrorAction Stop | Out-Null
    }
    else {
        New-IscsiTargetPortal -TargetPortalAddress $Portal -TargetPortalPortNumber $Port -ErrorAction Stop | Out-Null
    }
}

function Show-Targets {
    Add-Portal
    $targets = @(Get-IscsiTargetPortal -TargetPortalAddress $Portal | Get-IscsiTarget)
    if (-not $targets) { Write-Host "No targets at ${Portal}:$Port."; return }
    foreach ($t in $targets) {
        $name = $t.NodeAddress
        $sessions = @(Get-Sessions $name)
        if ($sessions) {
            $s = $sessions[0]
            Write-Host ('{0}: at most {1} connection(s); connected, {2} session(s), {3} connection(s)' -f
                $name, (Get-MaxConnections $s), $sessions.Count, @($s.ConnectionInformation).Count)
            continue
        }
        $auth = Get-AuthParameters
        try {
            $probe = Connect-IscsiTarget -NodeAddress $name -TargetPortalAddress $Portal `
                -TargetPortalPortNumber $Port -ReportToPnP $false -ErrorAction Stop @auth
        }
        catch {
            $why = if ($chap) { 'not in the ACL, or CHAP user or secret wrong' } else { 'not in the ACL, or CHAP required' }
            Write-Host "${name}: login refused ($why)"
            continue
        }
        $s = @(Get-Sessions $name) | Where-Object SessionId -eq $probe.SessionIdentifier
        $max = if ($s) { Get-MaxConnections $s } else { $null }
        if ($s) { Assert-Ok (Invoke-CimMethod -InputObject $s -MethodName Logout) 'Logout' }
        Write-Host ('{0}: at most {1} connection(s); not connected' -f $name, $max)
    }
}

if ($Show) { Show-State; return }
if ($List) { Show-Targets; return }
if ($Disconnect) {
    if (-not @(Get-Sessions $Target)) { Write-Host "No session to $Target."; return }
    Remove-Sessions $Target
    return
}

$sessions = @(Get-Sessions $Target)
if ($sessions.Count -gt 1 -and -not $Reconnect) {
    throw "There are $($sessions.Count) sessions to $Target. Use -Reconnect to log out all of them and start one."
}
if ($Reconnect) {
    Remove-Sessions $Target
    $sessions = @()
}
if (-not $sessions) {
    Write-Host "Logging in to $Target at ${Portal}:$Port"
    if (-not (Get-IscsiTargetPortal | Where-Object TargetPortalAddress -eq $Portal)) { Add-Portal }
    $auth = Get-AuthParameters
    try {
        Connect-IscsiTarget -NodeAddress $Target -TargetPortalAddress $Portal `
            -TargetPortalPortNumber $Port -ErrorAction Stop @auth | Out-Null
    }
    catch {
        $hint = if ($chap) { 'Are CHAP user and secret the ones set with "iscsictl chap set"?' }
                else { 'If the target requires CHAP, give -ChapUser.' }
        throw "The login to $Target failed ($_). $hint"
    }
    $sessions = @(Get-Sessions $Target)
    if ($sessions.Count -ne 1) { throw "Expected one session after the login, found $($sessions.Count)." }
}
$session = $sessions[0]

$max = Get-MaxConnections $session
if (-not $PSBoundParameters.ContainsKey('Connections')) {
    if (-not $max) { throw 'The maximum number of connections of the session is unknown; give -Connections.' }
    $Connections = $max
}
elseif ($max -and $Connections -gt $max) {
    throw "The target allows at most $max connection(s) per session (max_connections in its YAML)."
}
$have = @($session.ConnectionInformation).Count
$portalInfo = New-CimInstance -Namespace $ns -ClassName MSiSCSIInitiator_Portal -ClientOnly -Property @{
    Address = $Portal; Port = $Port; Index = [uint32]0; SymbolicName = '' }
$addArgs = @{ InitiatorPortNumber = $anyInitiatorPort; TargetPortal = $portalInfo; SecurityFlags = [uint64]0 }
if ($chap) { $addArgs.LoginOptions = New-LoginOptions }
while ($have -lt $Connections) {
    try {
        $r = Invoke-CimMethod -InputObject $session -MethodName AddConnection -Arguments $addArgs
    }
    catch {
        $hint = if ($chap) { '' } else { ' If the target requires CHAP, give -ChapUser.' }
        throw "Connection $($have + 1) could not be added ($_).$hint"
    }
    Assert-Ok $r "AddConnection (connection $($have + 1))"
    $have++
}
while ($have -gt $Connections) {
    # The connection with the lowest CID leads the session; it is never removed.
    $session = @(Get-Sessions $Target)[0]
    $last = @($session.ConnectionInformation) | Sort-Object { Get-Cid $_ } | Select-Object -Last 1
    $r = Invoke-CimMethod -InputObject $session -MethodName RemoveConnection -Arguments @{
        UniqueConnectionId = $last.ConnectionID }
    Assert-Ok $r 'RemoveConnection'
    $have--
}

$session = @(Get-Sessions $Target)[0]
$lb = Get-LbEntry $session
if (-not $lb) { throw "No load balance entry for session $($session.SessionId)." }
$lb.Entry.LoadBalancePolicy = [uint32]$codes[$Policy]
$ops = @(Get-CimInstance -Namespace $ns -ClassName MSiSCSI_LB_Operations |
    Where-Object InstanceName -eq $lb.Query.InstanceName)
if ($ops.Count -ne 1) { throw "No load balance operations for adapter $($lb.Query.InstanceName)." }
$r = Invoke-CimMethod -InputObject $ops[0] -MethodName SetLoadBalancePolicy -Arguments @{
    LoadBalancePolicies = $lb.Entry }
Assert-Ok $r 'SetLoadBalancePolicy'
if ($r.Status -ne 0) { throw "SetLoadBalancePolicy status $($r.Status)" }
Show-State
