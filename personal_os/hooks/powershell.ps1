# Personal OS terminal hook for Windows PowerShell 5.1+ (hook v1).
# Records, per command: the command line, working directory, start/end time, success and exit
# code. Nothing else: no output, no keystrokes. Lines starting with a space are never recorded,
# and nothing is written while Personal OS is paused or $env:PERSONAL_OS_PAUSE is set.
# Installed by `personal-os install-hooks`; removed by `personal-os uninstall-hooks`.

if ($global:__PersonalOS_Loaded) { return }
$global:__PersonalOS_Loaded = $true
$global:__PersonalOS_Spool = '__SPOOL__'
$global:__PersonalOS_Marker = '__MARKER__'
$global:__PersonalOS_Session = [guid]::NewGuid().ToString('N').Substring(0, 12)
$global:__PersonalOS_LastId = -1
$global:__PersonalOS_LastCode = $global:LASTEXITCODE
$global:__PersonalOS_Enc = New-Object System.Text.UTF8Encoding($false)
$global:__PersonalOS_File = [System.IO.Path]::Combine($global:__PersonalOS_Spool, "powershell-$PID.jsonl")
$global:__PersonalOS_Host = if ($env:WT_SESSION) { 'WindowsTerminal' } elseif ($env:TERM_PROGRAM -eq 'vscode') { 'vscode' } else { $Host.Name }
$global:__PersonalOS_OrigPrompt = $function:prompt

$global:__PersonalOS_Ctrl = New-Object System.Text.RegularExpressions.Regex '[\x00-\x1f]'
function global:__PersonalOS_Json([string]$s) {
    $s = $s.Replace('\', '\\').Replace('"', '\"').Replace("`r", '\r').Replace("`n", '\n').Replace("`t", '\t')
    if ($global:__PersonalOS_Ctrl.IsMatch($s)) {
        $s = $global:__PersonalOS_Ctrl.Replace($s, { param($m) '\u{0:x4}' -f [int][char]$m.Value })
    }
    $s
}

function global:prompt {
    $ok = $?
    $code = $global:LASTEXITCODE
    try {
        $h = Get-History -Count 1
        if ($h -and $h.Id -ne $global:__PersonalOS_LastId) {
            $global:__PersonalOS_LastId = $h.Id
            $line = $h.CommandLine
            if (-not $line.StartsWith(' ') -and -not $env:PERSONAL_OS_PAUSE -and
                -not [System.IO.File]::Exists($global:__PersonalOS_Marker)) {
                $exit = $null
                if ($code -ne $global:__PersonalOS_LastCode) { $exit = $code } elseif ($ok) { $exit = 0 }
                $fmt = "yyyy-MM-dd'T'HH:mm:ss.fff'Z'"
                # Hand-built JSON: ConvertTo-Json costs several ms per prompt in PowerShell 5.1.
                $cmd = __PersonalOS_Json $line
                $cwd = __PersonalOS_Json $PWD.ProviderPath
                $exitJson = if ($null -eq $exit) { 'null' } else { [string][int]$exit }
                $json = '{"v":1,"shell":"powershell","session":"' + $global:__PersonalOS_Session +
                    '","pid":' + $PID + ',"history_id":' + $h.Id + ',"command":"' + $cmd +
                    '","cwd":"' + $cwd + '","start":"' + $h.StartExecutionTime.ToUniversalTime().ToString($fmt) +
                    '","end":"' + $h.EndExecutionTime.ToUniversalTime().ToString($fmt) +
                    '","success":' + $(if ($ok) { 'true' } else { 'false' }) + ',"exit_code":' + $exitJson +
                    ',"host":"' + (__PersonalOS_Json $global:__PersonalOS_Host) + '"}'
                try {
                    [System.IO.File]::AppendAllText($global:__PersonalOS_File, $json + "`n", $global:__PersonalOS_Enc)
                } catch [System.IO.DirectoryNotFoundException] {
                    [System.IO.Directory]::CreateDirectory($global:__PersonalOS_Spool) | Out-Null
                    [System.IO.File]::AppendAllText($global:__PersonalOS_File, $json + "`n", $global:__PersonalOS_Enc)
                }
            }
        }
    } catch { }
    $global:__PersonalOS_LastCode = $code
    $global:LASTEXITCODE = $code
    & $global:__PersonalOS_OrigPrompt
}
