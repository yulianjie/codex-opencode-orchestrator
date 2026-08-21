#requires -Version 7.0

[CmdletBinding(DefaultParameterSetName = 'Inline')]
param(
    [Parameter(Mandatory)]
    [string]$Project,

    [Parameter(Mandatory, ParameterSetName = 'Inline')]
    [string]$Task,

    [Parameter(Mandatory, ParameterSetName = 'File')]
    [string]$TaskFile,

    [string[]]$Scope = @(),
    [string[]]$Constraint = @(),
    [string[]]$Acceptance = @(),
    [string[]]$VerifyCommand = @(),
    [string]$Model,

    [ValidateRange(1, 10)]
    [int]$MaxRounds = 2,

    [ValidateRange(30, 7200)]
    [int]$WorkerTimeoutSeconds = 900,

    [ValidateRange(1, 3600)]
    [int]$VerifyTimeoutSeconds = 300,

    [switch]$AllowDirty,
    [switch]$DryRun,
    [string]$ArtifactRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$utf8NoBom = [System.Text.UTF8Encoding]::new($false)

function Write-Utf8File {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][AllowEmptyString()][string]$Content
    )

    [System.IO.File]::WriteAllText($Path, $Content, $script:utf8NoBom)
}

function Convert-ToStringArray {
    param([AllowNull()]$Value)

    if ($null -eq $Value) {
        return @()
    }

    return @($Value | ForEach-Object { [string]$_ })
}

function Format-ListSection {
    param(
        [Parameter(Mandatory)][string]$Heading,
        [Parameter(Mandatory)][string[]]$Items,
        [string]$EmptyText = '- (none specified)'
    )

    $lines = if ($Items.Count -eq 0) {
        @($EmptyText)
    }
    else {
        @($Items | ForEach-Object { "- $_" })
    }

    return "$Heading`n$($lines -join "`n")"
}

function Start-CapturedProcess {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string[]]$ArgumentList,
        [Parameter(Mandatory)][string]$WorkingDirectory,
        [Parameter(Mandatory)][int]$TimeoutSeconds,
        [hashtable]$Environment = @{}
    )

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true
    $startInfo.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $startInfo.StandardErrorEncoding = [System.Text.Encoding]::UTF8

    foreach ($argument in $ArgumentList) {
        $null = $startInfo.ArgumentList.Add($argument)
    }

    foreach ($entry in $Environment.GetEnumerator()) {
        $startInfo.Environment[[string]$entry.Key] = [string]$entry.Value
    }

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    $startedAt = [DateTimeOffset]::UtcNow

    if (-not $process.Start()) {
        throw "Failed to start process: $FilePath"
    }

    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $completed = $process.WaitForExit($TimeoutSeconds * 1000)

    if (-not $completed) {
        try {
            $process.Kill($true)
        }
        catch {
            # The process may have exited between the timeout and Kill().
        }
        $process.WaitForExit()
    }

    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult()
    $duration = [Math]::Round(([DateTimeOffset]::UtcNow - $startedAt).TotalSeconds, 3)

    return [pscustomobject]@{
        ExitCode        = if ($completed) { $process.ExitCode } else { 124 }
        TimedOut        = -not $completed
        StandardOutput  = $stdout
        StandardError   = $stderr
        DurationSeconds = $duration
    }
}

function Invoke-Git {
    param(
        [Parameter(Mandatory)][string[]]$Arguments,
        [Parameter(Mandatory)][string]$WorkingDirectory
    )

    $git = Get-Command git -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $git) {
        return $null
    }

    return Start-CapturedProcess -FilePath $git.Source -ArgumentList $Arguments `
        -WorkingDirectory $WorkingDirectory -TimeoutSeconds 30
}

function Parse-OpenCodeEvents {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Text)

    $sessionId = $null
    $messages = [System.Collections.Generic.List[string]]::new()
    $toolCalls = [System.Collections.Generic.List[object]]::new()
    $parseErrors = [System.Collections.Generic.List[string]]::new()

    foreach ($line in ($Text -split "`r?`n")) {
        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }

        try {
            $event = $line | ConvertFrom-Json -Depth 100
            if ($event.PSObject.Properties.Name -contains 'sessionID' -and $event.sessionID) {
                $sessionId = [string]$event.sessionID
            }
            if (
                $event.PSObject.Properties.Name -contains 'part' -and
                $null -ne $event.part -and
                $event.part.PSObject.Properties.Name -contains 'type' -and
                $event.part.type -eq 'text' -and
                $event.part.PSObject.Properties.Name -contains 'text'
            ) {
                $messages.Add([string]$event.part.text)
            }
            if (
                $event.PSObject.Properties.Name -contains 'part' -and
                $null -ne $event.part -and
                $event.part.PSObject.Properties.Name -contains 'type' -and
                $event.part.type -eq 'tool' -and
                $event.part.PSObject.Properties.Name -contains 'tool'
            ) {
                $toolStatus = 'unknown'
                if (
                    $event.part.PSObject.Properties.Name -contains 'state' -and
                    $null -ne $event.part.state -and
                    $event.part.state.PSObject.Properties.Name -contains 'status'
                ) {
                    $toolStatus = [string]$event.part.state.status
                }
                $toolCalls.Add([pscustomobject]@{
                    tool   = [string]$event.part.tool
                    status = $toolStatus
                })
            }
        }
        catch {
            $parseErrors.Add($line)
        }
    }

    return [pscustomobject]@{
        SessionId  = $sessionId
        Message    = $messages -join "`n"
        ToolCalls  = @($toolCalls)
        ParseErrors = @($parseErrors)
    }
}

function Limit-FeedbackText {
    param(
        [AllowEmptyString()][string]$Text,
        [int]$MaximumLength = 12000
    )

    if ($Text.Length -le $MaximumLength) {
        return $Text
    }

    return $Text.Substring(0, $MaximumLength) + "`n... [output truncated by orchestrator]"
}

if ($PSCmdlet.ParameterSetName -eq 'File') {
    $resolvedTaskFile = (Resolve-Path -LiteralPath $TaskFile).Path
    $taskSpec = Get-Content -Raw -LiteralPath $resolvedTaskFile | ConvertFrom-Json -Depth 100
    if (-not ($taskSpec.PSObject.Properties.Name -contains 'task') -or [string]::IsNullOrWhiteSpace([string]$taskSpec.task)) {
        throw "Task file must contain a non-empty 'task' field."
    }

    $Task = [string]$taskSpec.task
    if (-not $PSBoundParameters.ContainsKey('Scope') -and $taskSpec.PSObject.Properties.Name -contains 'scope') {
        $Scope = Convert-ToStringArray $taskSpec.scope
    }
    if (-not $PSBoundParameters.ContainsKey('Constraint') -and $taskSpec.PSObject.Properties.Name -contains 'constraints') {
        $Constraint = Convert-ToStringArray $taskSpec.constraints
    }
    if (-not $PSBoundParameters.ContainsKey('Acceptance') -and $taskSpec.PSObject.Properties.Name -contains 'acceptance') {
        $Acceptance = Convert-ToStringArray $taskSpec.acceptance
    }
    if (-not $PSBoundParameters.ContainsKey('VerifyCommand') -and $taskSpec.PSObject.Properties.Name -contains 'verifyCommands') {
        $VerifyCommand = Convert-ToStringArray $taskSpec.verifyCommands
    }
    if (-not $PSBoundParameters.ContainsKey('Model') -and $taskSpec.PSObject.Properties.Name -contains 'model') {
        $Model = [string]$taskSpec.model
    }
    if (-not $PSBoundParameters.ContainsKey('MaxRounds') -and $taskSpec.PSObject.Properties.Name -contains 'maxRounds') {
        $MaxRounds = [int]$taskSpec.maxRounds
    }
}

if ([string]::IsNullOrWhiteSpace($Task)) {
    throw 'Task cannot be empty.'
}
if ($MaxRounds -lt 1 -or $MaxRounds -gt 10) {
    throw 'MaxRounds must be between 1 and 10.'
}

$resolvedProject = (Resolve-Path -LiteralPath $Project).Path
if (-not (Test-Path -LiteralPath $resolvedProject -PathType Container)) {
    throw "Project is not a directory: $resolvedProject"
}

$pwsh = Get-Command pwsh -CommandType Application -ErrorAction Stop | Select-Object -First 1
$openCodeScript = Get-Command opencode.ps1 -CommandType ExternalScript -ErrorAction Stop | Select-Object -First 1
$versionResult = Start-CapturedProcess -FilePath $pwsh.Source `
    -ArgumentList @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', $openCodeScript.Source, '--version') `
    -WorkingDirectory $resolvedProject -TimeoutSeconds 30
if ($versionResult.ExitCode -ne 0) {
    throw "OpenCode preflight failed: $($versionResult.StandardError)"
}
$openCodeVersion = $versionResult.StandardOutput.Trim()
[version]$parsedOpenCodeVersion = $null
if (
    [version]::TryParse($openCodeVersion, [ref]$parsedOpenCodeVersion) -and
    $parsedOpenCodeVersion -lt [version]'1.1.1'
) {
    throw "OpenCode 1.1.1 or newer is required for the permission model; found $openCodeVersion."
}

if ([string]::IsNullOrWhiteSpace($ArtifactRoot)) {
    $ArtifactRoot = Join-Path $PSScriptRoot 'runs'
}
$artifactRootPath = [System.IO.Path]::GetFullPath($ArtifactRoot)
$runId = [DateTimeOffset]::Now.ToString('yyyyMMdd-HHmmss') + '-' + ([Guid]::NewGuid().ToString('N').Substring(0, 8))
$runDirectory = Join-Path $artifactRootPath $runId

$gitProbe = Invoke-Git -Arguments @('-C', $resolvedProject, 'rev-parse', '--is-inside-work-tree') -WorkingDirectory $resolvedProject
$isGitRepository = $null -ne $gitProbe -and $gitProbe.ExitCode -eq 0 -and $gitProbe.StandardOutput.Trim() -eq 'true'
$gitStatusBefore = ''

if ($isGitRepository) {
    $statusResult = Invoke-Git -Arguments @('-C', $resolvedProject, 'status', '--porcelain=v1') -WorkingDirectory $resolvedProject
    if ($statusResult.ExitCode -ne 0) {
        throw "Could not read git status: $($statusResult.StandardError)"
    }
    $gitStatusBefore = $statusResult.StandardOutput.TrimEnd()
    if (-not $AllowDirty -and -not [string]::IsNullOrWhiteSpace($gitStatusBefore)) {
        throw "Project has existing changes. Re-run with -AllowDirty only after Codex has reviewed them.`n$gitStatusBefore"
    }
}

$null = [System.IO.Directory]::CreateDirectory($runDirectory)

$workerPermission = [ordered]@{
    '*'                = 'deny'
    read               = [ordered]@{
        '*'            = 'allow'
        '*.env'        = 'deny'
        '*.env.*'      = 'deny'
        '*.env.example' = 'allow'
    }
    edit               = 'allow'
    glob               = 'allow'
    grep               = 'allow'
    list               = 'allow'
    lsp                = 'allow'
    todowrite          = 'allow'
    bash               = 'deny'
    task               = 'deny'
    skill              = 'deny'
    webfetch           = 'deny'
    websearch          = 'deny'
    question           = 'deny'
    external_directory = 'deny'
    doom_loop          = 'deny'
}

$agentName = 'codex-worker-' + $runId.ToLowerInvariant()
$workerPrompt = @'
You are an implementation worker controlled by a Codex orchestrator.

Follow the task packet exactly. Inspect existing code before editing. Make the smallest complete change that satisfies the acceptance criteria, preserve unrelated work, and do not create commits. You are intentionally unable to run shell commands, access the network, launch subagents, load skills, or touch files outside the project. Codex runs verification independently and will return exact failures in a later round if needed.

Never claim that a command or test passed unless the task packet explicitly says Codex already ran it. End with these headings: STATUS, SUMMARY, FILES CHANGED, CHECKS NOT RUN, RISKS.
'@

$inlineConfig = [ordered]@{
    '$schema' = 'https://opencode.ai/config.json'
    agent     = [ordered]@{
        $agentName = [ordered]@{
            description = 'Restricted implementation worker controlled and verified by Codex'
            mode        = 'primary'
            steps       = 40
            prompt      = $workerPrompt
            permission  = $workerPermission
        }
    }
}
$inlineConfigJson = $inlineConfig | ConvertTo-Json -Depth 20 -Compress

$taskPacket = @(
    '# CODEX TASK PACKET'
    "Run ID: $runId"
    "Project: $resolvedProject"
    ''
    '## Goal'
    $Task.Trim()
    ''
    (Format-ListSection -Heading '## Allowed scope' -Items $Scope -EmptyText '- Current project only; infer the smallest relevant file set.')
    ''
    (Format-ListSection -Heading '## Constraints' -Items $Constraint -EmptyText '- Preserve unrelated behavior and existing user changes.')
    ''
    (Format-ListSection -Heading '## Acceptance criteria' -Items $Acceptance -EmptyText '- Implement the stated goal completely and report what changed.')
    ''
    (Format-ListSection -Heading '## Verification owned by Codex' -Items $VerifyCommand -EmptyText '- No command was supplied; Codex will inspect the resulting diff.')
    ''
    'Implement now. Do not merely propose a patch.'
) -join "`n"

Write-Utf8File -Path (Join-Path $runDirectory 'task-packet.md') -Content $taskPacket
Write-Utf8File -Path (Join-Path $runDirectory 'restricted-opencode-config.json') -Content ($inlineConfig | ConvertTo-Json -Depth 20)

$metadata = [ordered]@{
    runId            = $runId
    createdAt        = [DateTimeOffset]::Now.ToString('o')
    project          = $resolvedProject
    powerShellVersion = $PSVersionTable.PSVersion.ToString()
    openCodeVersion  = $openCodeVersion
    isGitRepository  = $isGitRepository
    allowDirty       = [bool]$AllowDirty
    gitStatusBefore  = $gitStatusBefore
    model            = $Model
    maxRounds        = $MaxRounds
    workerTimeoutSec = $WorkerTimeoutSeconds
    verifyTimeoutSec = $VerifyTimeoutSeconds
}
Write-Utf8File -Path (Join-Path $runDirectory 'metadata.json') -Content ($metadata | ConvertTo-Json -Depth 10)

Write-Host "Run:      $runId"
Write-Host "Project:  $resolvedProject"
Write-Host "Artifacts: $runDirectory"
Write-Host 'Boundary:  OpenCode may read/edit only inside the project; shell, web, subagents, skills, secrets, and external paths are denied.'

if ($DryRun) {
    Write-Host 'Dry run complete. OpenCode was not started.'
    [pscustomobject]@{
        status        = 'dry-run'
        runId         = $runId
        project       = $resolvedProject
        artifactPath  = $runDirectory
        taskPacket    = Join-Path $runDirectory 'task-packet.md'
    } | ConvertTo-Json -Depth 10
    return
}

$openCodeEnvironment = @{
    OPENCODE_CONFIG_CONTENT         = $inlineConfigJson
    OPENCODE_AUTO_SHARE             = 'false'
    OPENCODE_DISABLE_AUTOUPDATE     = 'true'
    OPENCODE_DISABLE_DEFAULT_PLUGINS = 'true'
    OPENCODE_DISABLE_CLAUDE_CODE    = 'true'
}

$sessionId = $null
$roundSummaries = [System.Collections.Generic.List[object]]::new()
$verificationResults = @()
$verificationHistory = [System.Collections.Generic.List[object]]::new()
$overallStatus = 'failed'
$nextPrompt = $taskPacket

for ($round = 1; $round -le $MaxRounds; $round++) {
    Write-Host "`n[Round $round/$MaxRounds] OpenCode is working..."

    $openCodeArguments = @(
        '-NoLogo', '-NoProfile', '-NonInteractive',
        '-File', $openCodeScript.Source,
        'run', '--pure', '--format', 'json',
        '--dir', $resolvedProject,
        '--agent', $agentName,
        '--title', "codex:$runId"
    )
    if (-not [string]::IsNullOrWhiteSpace($Model)) {
        $openCodeArguments += @('--model', $Model)
    }
    if (-not [string]::IsNullOrWhiteSpace($sessionId)) {
        $openCodeArguments += @('--session', $sessionId)
    }
    $openCodeArguments += $nextPrompt

    $workerResult = Start-CapturedProcess -FilePath $pwsh.Source -ArgumentList $openCodeArguments `
        -WorkingDirectory $resolvedProject -TimeoutSeconds $WorkerTimeoutSeconds `
        -Environment $openCodeEnvironment

    $eventsPath = Join-Path $runDirectory ("round-{0:D2}-events.ndjson" -f $round)
    $stderrPath = Join-Path $runDirectory ("round-{0:D2}-stderr.txt" -f $round)
    Write-Utf8File -Path $eventsPath -Content $workerResult.StandardOutput
    Write-Utf8File -Path $stderrPath -Content $workerResult.StandardError

    $parsed = Parse-OpenCodeEvents -Text $workerResult.StandardOutput
    if ($parsed.SessionId) {
        $sessionId = $parsed.SessionId
    }

    if (-not [string]::IsNullOrWhiteSpace($parsed.Message)) {
        Write-Host $parsed.Message
    }
    if ($parsed.ParseErrors.Count -gt 0) {
        Write-Warning "OpenCode emitted $($parsed.ParseErrors.Count) non-JSON line(s); inspect $eventsPath."
    }

    $allowedWorkerTools = @(
        'read', 'glob', 'grep', 'list', 'lsp', 'edit', 'write', 'patch',
        'apply_patch', 'todowrite', 'todoread'
    )
    $permissionViolations = @($parsed.ToolCalls | Where-Object {
        $_.status -eq 'completed' -and $_.tool -notin $allowedWorkerTools
    })
    $roundSummary = [ordered]@{
        round           = $round
        exitCode        = $workerResult.ExitCode
        timedOut        = $workerResult.TimedOut
        durationSeconds = $workerResult.DurationSeconds
        sessionId       = $sessionId
        workerMessage   = $parsed.Message
        tools           = @($parsed.ToolCalls)
        permissionViolations = $permissionViolations
        eventsPath      = $eventsPath
        stderrPath      = $stderrPath
    }
    $roundSummaries.Add([pscustomobject]$roundSummary)

    if ($permissionViolations.Count -gt 0) {
        $overallStatus = 'permission-violation'
        $forbiddenNames = ($permissionViolations | ForEach-Object { $_.tool }) -join ', '
        Write-Warning "OpenCode completed forbidden tool calls: $forbiddenNames. Verification was not accepted."
        break
    }

    if ($workerResult.ExitCode -ne 0) {
        $failureMessage = if ($workerResult.TimedOut) {
            "OpenCode timed out after $WorkerTimeoutSeconds seconds."
        }
        else {
            "OpenCode exited with code $($workerResult.ExitCode)."
        }
        Write-Warning $failureMessage
        if (-not [string]::IsNullOrWhiteSpace($workerResult.StandardError)) {
            Write-Warning (Limit-FeedbackText $workerResult.StandardError 4000)
        }
        break
    }

    $verificationResults = @()
    $allPassed = $true

    if ($VerifyCommand.Count -eq 0) {
        Write-Warning '[Verify] No command supplied; result is needs-review, not passed.'
        $overallStatus = 'needs-review'
        break
    }
    else {
        for ($verifyIndex = 0; $verifyIndex -lt $VerifyCommand.Count; $verifyIndex++) {
            $command = $VerifyCommand[$verifyIndex]
            Write-Host "[Verify $($verifyIndex + 1)/$($VerifyCommand.Count)] $command"
            $verifyResult = Start-CapturedProcess -FilePath $pwsh.Source `
                -ArgumentList @('-NoLogo', '-NoProfile', '-NonInteractive', '-Command', $command) `
                -WorkingDirectory $resolvedProject -TimeoutSeconds $VerifyTimeoutSeconds

            $verifyLogPath = Join-Path $runDirectory ("round-{0:D2}-verify-{1:D2}.txt" -f $round, ($verifyIndex + 1))
            $verifyLog = @(
                "COMMAND: $command"
                "EXIT CODE: $($verifyResult.ExitCode)"
                "TIMED OUT: $($verifyResult.TimedOut)"
                "DURATION SECONDS: $($verifyResult.DurationSeconds)"
                ''
                'STDOUT:'
                $verifyResult.StandardOutput
                ''
                'STDERR:'
                $verifyResult.StandardError
            ) -join "`n"
            Write-Utf8File -Path $verifyLogPath -Content $verifyLog

            $passed = $verifyResult.ExitCode -eq 0
            if (-not $passed) {
                $allPassed = $false
            }
            $verificationRecord = [pscustomobject]@{
                round           = $round
                command         = $command
                exitCode        = $verifyResult.ExitCode
                timedOut        = $verifyResult.TimedOut
                durationSeconds = $verifyResult.DurationSeconds
                passed          = $passed
                logPath         = $verifyLogPath
                stdout          = $verifyResult.StandardOutput
                stderr          = $verifyResult.StandardError
            }
            $verificationResults += $verificationRecord
            $verificationHistory.Add($verificationRecord)
            $verifyLabel = if ($passed) { '  PASS' } else { '  FAIL' }
            Write-Host $verifyLabel
        }
    }

    if ($allPassed) {
        $overallStatus = 'passed'
        break
    }

    if ($round -lt $MaxRounds) {
        $feedbackBlocks = $verificationResults | Where-Object { -not $_.passed } | ForEach-Object {
            @(
                "### Failed command"
                $_.command
                "Exit code: $($_.exitCode); timed out: $($_.timedOut)"
                'STDOUT:'
                (Limit-FeedbackText $_.stdout)
                'STDERR:'
                (Limit-FeedbackText $_.stderr)
            ) -join "`n"
        }

        $nextPrompt = @(
            '# CODEX VERIFICATION FEEDBACK'
            "Round $round failed independent verification."
            ''
            ($feedbackBlocks -join "`n`n")
            ''
            'Inspect the current files, fix the root cause within the original task scope, and report the updated files. Do not merely explain the failure.'
        ) -join "`n"
    }
}

$gitStatusAfter = ''
$gitDiffStat = ''
if ($isGitRepository) {
    $statusAfterResult = Invoke-Git -Arguments @('-C', $resolvedProject, 'status', '--short') -WorkingDirectory $resolvedProject
    $diffStatResult = Invoke-Git -Arguments @('-C', $resolvedProject, 'diff', '--stat') -WorkingDirectory $resolvedProject
    if ($statusAfterResult.ExitCode -eq 0) {
        $gitStatusAfter = $statusAfterResult.StandardOutput.TrimEnd()
    }
    if ($diffStatResult.ExitCode -eq 0) {
        $gitDiffStat = $diffStatResult.StandardOutput.TrimEnd()
    }
}

$publicVerificationResults = @($verificationHistory | ForEach-Object {
    [pscustomobject]@{
        round           = $_.round
        command         = $_.command
        exitCode        = $_.exitCode
        timedOut        = $_.timedOut
        durationSeconds = $_.durationSeconds
        passed          = $_.passed
        logPath         = $_.logPath
    }
})

$summary = [ordered]@{
    status             = $overallStatus
    runId              = $runId
    completedAt        = [DateTimeOffset]::Now.ToString('o')
    project            = $resolvedProject
    artifactPath       = $runDirectory
    openCodeSessionId  = $sessionId
    rounds             = @($roundSummaries)
    verification       = $publicVerificationResults
    gitStatusBefore    = $gitStatusBefore
    gitStatusAfter     = $gitStatusAfter
    gitDiffStat        = $gitDiffStat
}
$summaryPath = Join-Path $runDirectory 'summary.json'
Write-Utf8File -Path $summaryPath -Content ($summary | ConvertTo-Json -Depth 20)

Write-Host "`nResult: $overallStatus"
if (-not [string]::IsNullOrWhiteSpace($gitStatusAfter)) {
    Write-Host "Changed files:`n$gitStatusAfter"
}
Write-Host "Summary: $summaryPath"

$summary | ConvertTo-Json -Depth 20
if ($overallStatus -ne 'passed') {
    exit 1
}
