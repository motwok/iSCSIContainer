<#
.SYNOPSIS
  Opens, shows and closes iSCSI sessions over several network links on the Windows initiator, with
  MC/S (several connections in one session) or MPIO (several sessions), without the iscsicpl
  dialog.

.DESCRIPTION
  Uses the Microsoft iSCSI initiator (its WMI interface in root/wmi and the iSCSI cmdlets) and,
  where installed, MPIO. Run it in an elevated PowerShell.

  Mode: MPIO is ready when Multipath-IO is installed and the Microsoft DSM claims iSCSI disks,
  whatever the Windows edition. -Mode Auto (default) uses MPIO when that is ready and MC/S
  otherwise. MC/S and MPIO are never mixed for one target; changing the mode needs -Reconnect.

  MC/S: without a session to the target it logs in; with one it adds or removes connections until
  the wanted number is reached, then sets the MCS load balance policy. A new session starts with
  Round Robin, so the policy is always set explicitly. Without -Connections it opens as many
  connections as the target allows (max_connections in its YAML).

  MPIO: it opens one session with one connection per wanted path. Before a second session is
  opened, every disk of the target must be claimed by MPIO; several sessions to a disk without
  MPIO look like independent disks and destroy its data. The policy is set per MPIO disk. Without
  -Connections it opens 16 sessions.

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
  Number of TCP connections to the target. MC/S: connections of the one session, default the
  maximum the target allows. MPIO: number of sessions, default 16.

.PARAMETER Policy
  Load balance policy: LeastQueueDepth (default), RoundRobin or FailOverOnly. MC/S only; with
  MPIO the script sets LeastQueueDepth, since the Microsoft DSM refused the others for the LUNs of
  the iSCSIContainer target (they report ALUA).

.PARAMETER Mode
  Auto (default): MPIO if it is ready, else MC/S. Mcs or Mpio force one; Mpio stops if MPIO is
  not ready.

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
    [ValidateSet('Auto', 'Mcs', 'Mpio')]
    [string]$Mode = 'Auto',

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
# DSM_Load_Balance_Policy_V2.LoadBalancePolicy; the codes 1 to 5 are those of mpclaim -l as well.
$mpioNames = @{ 1 = 'Fail Over Only'; 2 = 'Round Robin'; 3 = 'Round Robin with Subset'
    4 = 'Least Queue Depth'; 5 = 'Weighted Paths'; 6 = 'Least Blocks' }
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

# Why MPIO cannot be used here, or $null if it can.
function Get-MpioProblem {
    $dsm = Get-CimInstance Win32_SystemDriver -Filter "Name='msdsm'" -ErrorAction SilentlyContinue
    if (-not $dsm -or -not (Get-Command Get-MSDSMAutomaticClaimSettings -ErrorAction SilentlyContinue)) {
        return 'Multipath-IO is not installed (Windows Server: Install-WindowsFeature Multipath-IO, then restart)'
    }
    $claim = Get-MSDSMAutomaticClaimSettings -ErrorAction SilentlyContinue
    if (-not $claim -or -not $claim['iSCSI']) {
        return 'MPIO does not claim iSCSI disks (Enable-MSDSMAutomaticClaim -BusType iSCSI, then restart)'
    }
    $null
}

# Disks of the target, one per LUN.
function Get-TargetDisks {
    @(Get-IscsiSession -ErrorAction SilentlyContinue | Where-Object TargetNodeAddress -eq $Target |
        Get-Disk -ErrorAction SilentlyContinue | Sort-Object Number -Unique)
}

function Test-MpioDisk($disk) {
    $disk.Path -like '\\?\mpio#*'
}

# MPIO_DISK_INFO entry of a disk: Name "MPIO Disk<n>", NumberPaths. The disk's UniqueId is the
# entry's SerialNumber.
function Get-MpioEntry($disk) {
    foreach ($info in @(Get-CimInstance -Namespace $ns -ClassName MPIO_DISK_INFO -ErrorAction SilentlyContinue)) {
        foreach ($d in @($info.DriveInfo)) {
            if ($d.SerialNumber -eq $disk.UniqueId) { return $d }
        }
    }
    $null
}

function Get-MpioNumber($entry) {
    [int]($entry.Name -replace '\D', '')
}

# Policy of an MPIO disk from DSM_QueryLBPolicy_V2. Its InstanceName is the disk's MPIO path
# ("\\?\mpio#disk&...#1&...#{guid}" becomes "MPIO\Disk&...\1&..._0").
function Get-MpioPolicy($disk) {
    $parts = $disk.Path.Substring(4) -split '#'
    $want = ($parts[0..2] -join '\').ToUpperInvariant()
    foreach ($q in @(Get-CimInstance -Namespace $ns -ClassName DSM_QueryLBPolicy_V2 -ErrorAction SilentlyContinue)) {
        if (($q.InstanceName -replace '_\d+$', '').ToUpperInvariant() -eq $want) {
            return $mpioNames[[int]$q.LoadBalancePolicy.LoadBalancePolicy]
        }
    }
    'unknown'
}

function Show-State {
    $sessions = @(Get-Sessions $Target)
    if (-not $sessions) { Write-Host "No session to $Target."; return }
    $disks = @(Get-TargetDisks)
    if (@($disks | Where-Object { Test-MpioDisk $_ })) {
        Write-Host ('{0}: {1} session(s) with MPIO' -f $Target, $sessions.Count)
        foreach ($disk in $disks) {
            $entry = Get-MpioEntry $disk
            if (-not $entry) { Write-Host "  Disk $($disk.Number): NOT claimed by MPIO"; continue }
            Write-Host ('  Disk {0} ({1}): {2} path(s), policy {3}' -f $disk.Number, $entry.Name,
                $entry.NumberPaths, (Get-MpioPolicy $disk))
        }
        return
    }
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

# One login to the target; -IsMultipathEnabled for MPIO, so Windows allows further sessions.
function Connect-Session([bool]$multipath) {
    if (-not (Get-IscsiTargetPortal | Where-Object TargetPortalAddress -eq $Portal)) { Add-Portal }
    $auth = Get-AuthParameters
    try {
        Connect-IscsiTarget -NodeAddress $Target -TargetPortalAddress $Portal -TargetPortalPortNumber $Port `
            -IsMultipathEnabled $multipath -ErrorAction Stop @auth | Out-Null
    }
    catch {
        $hint = if ($chap) { 'Are CHAP user and secret the ones set with "iscsictl chap set"?' }
                else { 'If the target requires CHAP, give -ChapUser.' }
        throw "The login to $Target failed ($_). $hint"
    }
}

function Set-Mcs {
    $sessions = @(Get-Sessions $Target)
    if ($sessions.Count -gt 1) {
        throw "There are $($sessions.Count) sessions to $Target (MPIO?). Use -Reconnect to log out all of them and start one MC/S session."
    }
    $fresh = $false
    if (-not $sessions) {
        Write-Host "Logging in to $Target at ${Portal}:$Port (MC/S)"
        Connect-Session $false
        $sessions = @(Get-Sessions $Target)
        if ($sessions.Count -ne 1) { throw "Expected one session after the login, found $($sessions.Count)." }
        $fresh = $true
    }
    $session = $sessions[0]

    # The maximum is only known after the login; a session opened just for this is closed again.
    $max = Get-MaxConnections $session
    $count = $Connections
    $problem = $null
    if (-not $script:connectionsGiven) {
        if ($max) { $count = $max }
        else { $problem = 'The maximum number of connections of the session is unknown; give -Connections.' }
    }
    elseif ($max -and $count -gt $max) {
        $problem = "The target allows at most $max connection(s) per session (max_connections in its YAML)."
    }
    if ($problem) {
        if ($fresh) { Assert-Ok (Invoke-CimMethod -InputObject $session -MethodName Logout) 'Logout' }
        throw $problem
    }
    $have = @($session.ConnectionInformation).Count
    $portalInfo = New-CimInstance -Namespace $ns -ClassName MSiSCSIInitiator_Portal -ClientOnly -Property @{
        Address = $Portal; Port = $Port; Index = [uint32]0; SymbolicName = '' }
    $addArgs = @{ InitiatorPortNumber = $anyInitiatorPort; TargetPortal = $portalInfo; SecurityFlags = [uint64]0 }
    if ($chap) { $addArgs.LoginOptions = New-LoginOptions }
    while ($have -lt $count) {
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
    while ($have -gt $count) {
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
}

# Waits until the target's disks are there and their number stays the same for two seconds.
function Wait-TargetDisks {
    $last = -1
    $stable = 0
    for ($i = 0; $i -lt 30; $i++) {
        $n = @(Get-TargetDisks).Count
        if ($n -gt 0 -and $n -eq $last) {
            $stable++
            if ($stable -ge 2) { return }
        }
        else { $stable = 0 }
        $last = $n
        Start-Sleep -Seconds 1
    }
}

function Set-Mpio {
    $sessions = @(Get-Sessions $Target)
    if (@($sessions | Where-Object { @($_.ConnectionInformation).Count -gt 1 })) {
        throw "The session to $Target has several connections (MC/S). Use -Reconnect to switch to MPIO."
    }
    $count = if ($script:connectionsGiven) { $Connections } else { 16 }
    if (-not $sessions) {
        Write-Host "Logging in to $Target at ${Portal}:$Port (MPIO)"
        Connect-Session $true
    }
    Wait-TargetDisks
    $disks = @(Get-TargetDisks)
    if (-not $disks) { throw "The target exports no disk to this initiator." }
    foreach ($disk in $disks) {
        if (-not (Test-MpioDisk $disk)) {
            throw "Disk $($disk.Number) of $Target is not claimed by MPIO; no further session is opened. Check Get-MSDSMAutomaticClaimSettings."
        }
    }
    while (@(Get-Sessions $Target).Count -lt $count) { Connect-Session $true }
    while (@(Get-Sessions $Target).Count -gt $count) {
        $last = @(Get-Sessions $Target) | Sort-Object { Get-IdNumber $_.SessionId } | Select-Object -Last 1
        Assert-Ok (Invoke-CimMethod -InputObject $last -MethodName Logout) 'Logout'
    }
    # Wait until MPIO counts every path, then set the policy per MPIO disk.
    foreach ($disk in $disks) {
        for ($i = 0; $i -lt 30; $i++) {
            $entry = Get-MpioEntry $disk
            if ($entry -and $entry.NumberPaths -eq $count) { break }
            Start-Sleep -Seconds 1
        }
        if (-not $entry) { throw "Disk $($disk.Number) has no MPIO entry." }
        $out = mpclaim.exe -l -d (Get-MpioNumber $entry) $codes[$Policy] 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "The policy $Policy could not be set on $($entry.Name): $($out -join ' ')"
        }
    }
}

if ($Show) { Show-State; return }
if ($List) { Show-Targets; return }
if ($Disconnect) {
    if (-not @(Get-Sessions $Target)) { Write-Host "No session to $Target."; return }
    Remove-Sessions $Target
    return
}

$script:connectionsGiven = $PSBoundParameters.ContainsKey('Connections')
$problem = Get-MpioProblem
$use = switch ($Mode) {
    'Mpio' { if ($problem) { throw "MPIO cannot be used: $problem" }; 'Mpio' }
    'Mcs' { 'Mcs' }
    default { if ($problem) { 'Mcs' } else { 'Mpio' } }
}
if ($Mode -eq 'Auto' -and $problem) {
    Write-Host "Using MC/S: $problem"
}
if ($use -eq 'Mpio' -and $Policy -ne 'LeastQueueDepth') {
    throw 'With MPIO only -Policy LeastQueueDepth is supported (the Microsoft DSM refuses the others for ALUA LUNs).'
}
if ($Reconnect) { Remove-Sessions $Target }
if ($use -eq 'Mpio') { Set-Mpio } else { Set-Mcs }
Show-State
