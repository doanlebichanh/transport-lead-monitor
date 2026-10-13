[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot "sources.json"),
    [string]$ReportDirectory = (Join-Path $PSScriptRoot "reports"),
    [string]$Message = "Daily reefer transport leads",
    [switch]$NoTelegram,
    [switch]$InstallScheduledTask,
    [string]$TaskTime = "07:00"
)

$ErrorActionPreference = "Stop"
$script:LogPath = $null

function Write-RunLog {
    param(
        [string]$Text,
        [string]$Level = "INFO"
    )

    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Text
    Write-Host $line
    if ($script:LogPath) {
        Add-Content -LiteralPath $script:LogPath -Value $line -Encoding UTF8
    }
}

function ConvertTo-SearchText {
    param([string]$Text)

    if ([string]::IsNullOrWhiteSpace($Text)) {
        return ""
    }

    $normalized = $Text.Replace("đ", "d").Replace("Đ", "D").Normalize([Text.NormalizationForm]::FormD)
    return [regex]::Replace($normalized, "\p{Mn}", "").ToLowerInvariant()
}

function Get-FeedItems {
    param([pscustomobject]$Source)

    $response = Invoke-WebRequest `
        -Uri $Source.Url `
        -UseBasicParsing `
        -TimeoutSec 30 `
        -Headers @{ "User-Agent" = "TransportLeadReport/1.0" }

    $settings = New-Object System.Xml.XmlReaderSettings
    $settings.DtdProcessing = [System.Xml.DtdProcessing]::Prohibit
    $settings.XmlResolver = $null
    $textReader = [System.IO.StringReader]::new($response.Content)
    $xmlReader = [System.Xml.XmlReader]::Create([System.IO.TextReader]$textReader, [System.Xml.XmlReaderSettings]$settings)
    try {
        $document = New-Object System.Xml.XmlDocument
        $document.XmlResolver = $null
        $document.Load([System.Xml.XmlReader]$xmlReader)
    }
    finally {
        $xmlReader.Dispose()
        $textReader.Dispose()
    }

    $nodes = $document.SelectNodes("//*[local-name()='item' or local-name()='entry']")
    foreach ($node in $nodes) {
        $titleNode = $node.SelectSingleNode("./*[local-name()='title']")
        $descriptionNode = $node.SelectSingleNode("./*[local-name()='description' or local-name()='summary' or local-name()='encoded' or local-name()='content']")
        $linkNode = $node.SelectSingleNode("./*[local-name()='link']")
        $dateNode = $node.SelectSingleNode("./*[local-name()='pubDate' or local-name()='published' or local-name()='updated' or local-name()='date']")

        $title = if ($titleNode) { $titleNode.InnerText.Trim() } else { "" }
        $description = if ($descriptionNode) { $descriptionNode.InnerText } else { "" }
        $link = ""
        if ($linkNode) {
            $link = $linkNode.GetAttribute("href")
            if ([string]::IsNullOrWhiteSpace($link)) {
                $link = $linkNode.InnerText.Trim()
            }
        }
        $publishedRaw = if ($dateNode) { $dateNode.InnerText.Trim() } else { "" }
        $plainDescription = [System.Net.WebUtility]::HtmlDecode(([regex]::Replace($description, "<[^>]+>", " ")))

        [pscustomobject]@{
            Title = $title
            Description = $plainDescription
            Link = $link
            PublishedRaw = $publishedRaw
        }
    }
}

function Send-TelegramDocument {
    param(
        [string]$Path,
        [string]$Caption
    )

    $token = $env:TELEGRAM_BOT_TOKEN
    $chatIds = @($env:TELEGRAM_CHAT_IDS -split ";" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ([string]::IsNullOrWhiteSpace($token)) {
        throw "TELEGRAM_BOT_TOKEN is not set for this Windows account."
    }
    if ($chatIds.Count -eq 0) {
        throw "TELEGRAM_CHAT_IDS is empty. Use semicolon-separated chat IDs."
    }

    [void][System.Reflection.Assembly]::LoadWithPartialName("System.Net.Http")
    foreach ($chatId in $chatIds) {
        $client = New-Object System.Net.Http.HttpClient
        $form = New-Object System.Net.Http.MultipartFormDataContent
        $fileStream = $null
        try {
            $form.Add([System.Net.Http.StringContent]::new($chatId), "chat_id")
            $form.Add([System.Net.Http.StringContent]::new($Caption), "caption")
            $fileStream = [System.IO.File]::OpenRead($Path)
            $fileContent = [System.Net.Http.StreamContent]::new($fileStream)
            $fileContent.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse("text/csv")
            $form.Add($fileContent, "document", [System.IO.Path]::GetFileName($Path))

            $uri = "https://api.telegram.org/bot{0}/sendDocument" -f $token
            $response = $client.PostAsync($uri, $form).GetAwaiter().GetResult()
            $responseText = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
            if (-not $response.IsSuccessStatusCode) {
                throw "Telegram HTTP error for chat ${chatId}: $responseText"
            }
            $telegramResult = $responseText | ConvertFrom-Json
            if (-not $telegramResult.ok) {
                throw "Telegram rejected the report for chat ${chatId}: $responseText"
            }
            Write-RunLog "Report sent to chat $chatId."
        }
        finally {
            if ($fileStream) { $fileStream.Dispose() }
            $form.Dispose()
            $client.Dispose()
        }
    }
}

try {
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        throw "Source configuration not found: $ConfigPath"
    }

    $configuration = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $configuration.Sources -or @($configuration.Sources).Count -eq 0) {
        throw "No sources configured. Add permitted RSS/Atom feed URLs to $ConfigPath."
    }

    if ($InstallScheduledTask) {
        $startTime = [datetime]::ParseExact($TaskTime, "HH:mm", [Globalization.CultureInfo]::InvariantCulture)
        $powerShellExe = Join-Path $PSHOME "powershell.exe"
        $taskArguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -ConfigPath "{1}" -ReportDirectory "{2}"' -f $PSCommandPath, $ConfigPath, $ReportDirectory
        $action = New-ScheduledTaskAction -Execute $powerShellExe -Argument $taskArguments
        $trigger = New-ScheduledTaskTrigger -Daily -At $startTime
        Register-ScheduledTask `
            -TaskName "Daily Reefer Transport Leads" `
            -Action $action `
            -Trigger $trigger `
            -Description "Collect configured public RSS/Atom transport leads and send a CSV report." `
            -Force | Out-Null
        Write-Host "Scheduled task installed for $TaskTime. It runs under the current Windows account."
        return
    }

    New-Item -ItemType Directory -Path $ReportDirectory -Force | Out-Null
    $logDirectory = Join-Path $PSScriptRoot "logs"
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $script:LogPath = Join-Path $logDirectory ("daily-transport_{0}.log" -f (Get-Date -Format "yyyyMMdd"))

    $vehicleTerms = @($configuration.VehicleTerms)
    $routeTerms = @($configuration.RouteTerms)
    if ($vehicleTerms.Count -eq 0 -or $routeTerms.Count -eq 0) {
        throw "VehicleTerms and RouteTerms must each contain at least one term in the configuration."
    }

    $vehiclePattern = "45\s*rf|48\s*rf|" + (($vehicleTerms | ForEach-Object { [regex]::Escape((ConvertTo-SearchText $_)) }) -join "|")
    $routePattern = ($routeTerms | ForEach-Object { [regex]::Escape((ConvertTo-SearchText $_)) }) -join "|"
    $phonePattern = "(?<!\d)(?:\+?84|0)(?:[\s().-]*\d){8,10}(?!\d)"
    $seen = @{}
    $leads = New-Object System.Collections.Generic.List[object]
    $successfulSources = 0

    foreach ($source in @($configuration.Sources)) {
        if ([string]::IsNullOrWhiteSpace($source.Name) -or [string]::IsNullOrWhiteSpace($source.Url)) {
            Write-RunLog "Skipped a source without both Name and Url." "WARN"
            continue
        }

        try {
            $feedItems = @(Get-FeedItems -Source $source)
            $successfulSources++
            Write-RunLog ("Read {0} items from {1}." -f $feedItems.Count, $source.Name)
        }
        catch {
            Write-RunLog ("Could not read {0}: {1}" -f $source.Name, $_.Exception.Message) "ERROR"
            continue
        }

        foreach ($item in $feedItems) {
            $content = "{0} {1}" -f $item.Title, $item.Description
            $searchable = ConvertTo-SearchText $content
            if ($searchable -notmatch $vehiclePattern -or $searchable -notmatch $routePattern) {
                continue
            }

            $dedupeKey = if ($item.Link) { $item.Link.Trim().ToLowerInvariant() } else { $item.Title.Trim().ToLowerInvariant() }
            if ($seen.ContainsKey($dedupeKey)) {
                continue
            }
            $seen[$dedupeKey] = $true

            $published = $item.PublishedRaw
            if ($published) {
                try { $published = [DateTimeOffset]::Parse($published).ToLocalTime().ToString("yyyy-MM-dd HH:mm") }
                catch { }
            }
            $matches = @([regex]::Matches($content, $phonePattern) | ForEach-Object { $_.Value } | Select-Object -Unique)
            $snippet = [regex]::Replace($item.Description, "\s+", " ").Trim()
            if ($snippet.Length -gt 400) { $snippet = $snippet.Substring(0, 400) }

            $leads.Add([pscustomobject]@{
                Published = $published
                Source = $source.Name
                Title = $item.Title
                ContactPhones = ($matches -join "; ")
                VehicleTerms = (($vehicleTerms | Where-Object { $searchable -match [regex]::Escape((ConvertTo-SearchText $_)) }) -join "; ")
                RouteTerms = (($routeTerms | Where-Object { $searchable -match [regex]::Escape((ConvertTo-SearchText $_)) }) -join "; ")
                Snippet = $snippet
                Url = $item.Link
            })
        }
    }

    if ($successfulSources -eq 0) {
        throw "No configured source could be read; no report was sent. Check the feed URLs and network access."
    }

    $reportPath = Join-Path $ReportDirectory ("transport_leads_{0}.csv" -f (Get-Date -Format "yyyyMMdd"))
    $columns = @('"Published"','"Source"','"Title"','"ContactPhones"','"VehicleTerms"','"RouteTerms"','"Snippet"','"Url"')
    Set-Content -LiteralPath $reportPath -Value ($columns -join ",") -Encoding UTF8
    if ($leads.Count -gt 0) {
        $leads | Export-Csv -LiteralPath $reportPath -NoTypeInformation -Append -Encoding UTF8
    }

    Write-RunLog ("Report created with {0} matching leads: {1}" -f $leads.Count, $reportPath)
    if (-not $NoTelegram) {
        Send-TelegramDocument -Path $reportPath -Caption $Message
    }
    else {
        Write-RunLog "Telegram delivery skipped by -NoTelegram."
    }
}
catch {
    Write-RunLog $_.Exception.Message "ERROR"
    exit 1
}