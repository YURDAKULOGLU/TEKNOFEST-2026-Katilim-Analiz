[CmdletBinding()]
param(
    [switch]$SkipModelPull,
    [switch]$SkipBuild
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ClusterName = "katilim-analiz"
$Context = "kind-$ClusterName"
$Namespace = "katilim-analiz"
$Image = "ghcr.io/yurdakuloglu/katilim-analiz:dev"
$ExpectedKindVersion = "v0.32.0"
$ExpectedKubernetesVersion = "v1.34.8"
$ExpectedKubectlMinor = 34

function Resolve-RequiredCommand {
    param([Parameter(Mandatory)][string]$Name)

    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }
    if ($Name -eq "kind") {
        $packageRoot = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
        if (Test-Path -LiteralPath $packageRoot) {
            $candidate = Get-ChildItem -LiteralPath $packageRoot -Filter kind.exe -File -Recurse |
                Select-Object -First 1
            if ($null -ne $candidate) {
                return $candidate.FullName
            }
        }
    }
    throw "Required command '$Name' was not found on PATH."
}

function Invoke-Kubectl {
    & $script:KubectlExe --context $script:Context @args
    if ($LASTEXITCODE -ne 0) {
        throw "kubectl failed: $($args -join ' ')"
    }
}

function Get-ConfiguredModelIdentity {
    $configPath = Join-Path $script:RepoRoot "deploy/k8s/base/config-map.yaml"
    $config = Get-Content -LiteralPath $configPath -Raw
    $modelMatches = [regex]::Matches(
        $config,
        '(?m)^\s*OLLAMA_MODEL:\s*["'']?(?<model>[A-Za-z0-9._/-]+:[A-Za-z0-9._-]+)["'']?\s*$'
    )
    $digestMatches = [regex]::Matches(
        $config,
        '(?m)^\s*OLLAMA_MODEL_DIGEST:\s*["'']?(?<digest>[0-9a-f]{64})["'']?\s*$'
    )
    if ($modelMatches.Count -ne 1 -or $digestMatches.Count -ne 1) {
        throw "The base ConfigMap must contain exactly one explicit OLLAMA_MODEL and one lowercase 64-hex OLLAMA_MODEL_DIGEST."
    }
    return [pscustomobject]@{
        Name = $modelMatches[0].Groups['model'].Value
        Digest = $digestMatches[0].Groups['digest'].Value
    }
}

function Assert-OllamaInventory {
    param(
        [Parameter(Mandatory)]$Inventory,
        [Parameter(Mandatory)][string]$ExpectedModel,
        [Parameter(Mandatory)][string]$ExpectedDigest
    )

    if ($ExpectedModel -notmatch '^[A-Za-z0-9._/-]+:[A-Za-z0-9._-]+$') {
        throw "Configured Ollama model name is not explicit: '$ExpectedModel'."
    }
    if ($ExpectedDigest -notmatch '^[0-9a-f]{64}$') {
        throw "Configured Ollama model digest is not a lowercase 64-hex SHA-256 value."
    }
    $modelsProperty = $Inventory.PSObject.Properties['models']
    if ($null -eq $modelsProperty -or $null -eq $modelsProperty.Value) {
        throw "Ollama /api/tags response has no models array."
    }
    $matches = @($modelsProperty.Value | Where-Object {
        $null -ne $_.PSObject.Properties['name'] -and [string]$_.name -ceq $ExpectedModel
    })
    if ($matches.Count -ne 1) {
        throw "Ollama /api/tags must contain '$ExpectedModel' exactly once; found $($matches.Count)."
    }
    $digestProperty = $matches[0].PSObject.Properties['digest']
    $actualDigest = if ($null -eq $digestProperty) { "" } else { [string]$digestProperty.Value }
    if ($actualDigest -notmatch '^[0-9a-f]{64}$') {
        throw "Ollama returned an invalid digest for '$ExpectedModel'."
    }
    if ($actualDigest -cne $ExpectedDigest) {
        throw "Ollama model '$ExpectedModel' has digest '$actualDigest', expected '$ExpectedDigest'."
    }
}

function Assert-ConfiguredOllamaModel {
    param(
        [Parameter(Mandatory)][string]$ExpectedModel,
        [Parameter(Mandatory)][string]$ExpectedDigest,
        [switch]$SkippedPull
    )

    $endpoint = "/api/v1/namespaces/$Namespace/services/http:ollama:11434/proxy/api/tags"
    $output = @(& $script:KubectlExe --context $script:Context get --raw $endpoint 2>&1)
    if ($LASTEXITCODE -ne 0) {
        $mode = if ($SkippedPull) { " after -SkipModelPull" } else { "" }
        throw "Unable to read the Ollama model inventory$mode`: $($output -join [Environment]::NewLine)"
    }
    try {
        $inventory = ($output -join "`n") | ConvertFrom-Json
    }
    catch {
        throw "Ollama /api/tags returned invalid JSON: $($_.Exception.Message)"
    }
    Assert-OllamaInventory `
        -Inventory $inventory -ExpectedModel $ExpectedModel -ExpectedDigest $ExpectedDigest
    Write-Host "Verified exact Ollama model: $ExpectedModel@$ExpectedDigest" -ForegroundColor Green
}

function Reset-And-ApplyJob {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Manifest,
        [Parameter(Mandatory)][string]$Timeout
    )

    Invoke-Kubectl -n $Namespace delete job $Name --ignore-not-found=true
    Invoke-Kubectl -n $Namespace apply -f $Manifest
    Invoke-Kubectl -n $Namespace wait --for=condition=complete "job/$Name" "--timeout=$Timeout"
    Invoke-Kubectl -n $Namespace logs "job/$Name"
}

$DockerExe = Resolve-RequiredCommand -Name "docker"
$script:KubectlExe = Resolve-RequiredCommand -Name "kubectl"
$KindExe = Resolve-RequiredCommand -Name "kind"
$modelIdentity = Get-ConfiguredModelIdentity

$kindVersionOutput = (& $KindExe version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $kindVersionOutput -notmatch [regex]::Escape($ExpectedKindVersion)) {
    throw "kind $ExpectedKindVersion is required; found '$kindVersionOutput'."
}

$kubectlVersion = & $script:KubectlExe version --client -o json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) {
    throw "Unable to determine the kubectl client version."
}
$kubectlMinorText = [string]$kubectlVersion.clientVersion.minor
$kubectlMinorMatch = [regex]::Match($kubectlMinorText, '\d+')
if (-not $kubectlMinorMatch.Success) {
    throw "Unexpected kubectl minor version '$kubectlMinorText'."
}
$kubectlMinor = [int]$kubectlMinorMatch.Value
if ($kubectlMinor -ne $ExpectedKubectlMinor) {
    throw "kubectl minor $ExpectedKubectlMinor is required for the pinned Kubernetes 1.34 baseline; found $kubectlMinor."
}

Push-Location $RepoRoot
try {
    & $DockerExe info *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker engine is not available."
    }

    $clusters = @(& $KindExe get clusters 2>$null)
    if ($clusters -notcontains $ClusterName) {
        & $KindExe create cluster --config deploy/kind/cluster.yaml --wait 180s
        if ($LASTEXITCODE -ne 0) {
            throw "Kind cluster creation failed."
        }
    }

    $nodeList = Invoke-Kubectl get nodes -o json | ConvertFrom-Json
    $unexpectedNodeVersions = @(
        $nodeList.items |
            Where-Object { $_.status.nodeInfo.kubeletVersion -ne $ExpectedKubernetesVersion } |
            ForEach-Object { "$($_.metadata.name)=$($_.status.nodeInfo.kubeletVersion)" }
    )
    if ($unexpectedNodeVersions.Count -gt 0) {
        throw "Existing Kind cluster does not match ${ExpectedKubernetesVersion}: $($unexpectedNodeVersions -join ', '). Delete/recreate it explicitly after preserving any needed data."
    }

    Invoke-Kubectl apply -k deploy/k8s/overlays/local-infra
    Invoke-Kubectl -n $Namespace rollout status statefulset/postgres --timeout=180s
    Invoke-Kubectl -n $Namespace rollout status deployment/ollama --timeout=180s

    if (-not $SkipModelPull) {
        Reset-And-ApplyJob `
            -Name "model-pull" `
            -Manifest "deploy/k8s/components/local-services/model-pull-job.yaml" `
            -Timeout "20m"
    }
    Assert-ConfiguredOllamaModel `
        -ExpectedModel $modelIdentity.Name `
        -ExpectedDigest $modelIdentity.Digest `
        -SkippedPull:$SkipModelPull
    Reset-And-ApplyJob `
        -Name "model-warmup" `
        -Manifest "deploy/k8s/operations/model-warmup-job.yaml" `
        -Timeout "5m"

    if (-not $SkipBuild) {
        & $DockerExe build --tag $Image .
        if ($LASTEXITCODE -ne 0) {
            throw "Application image build failed."
        }
        & $KindExe load docker-image $Image --name $ClusterName
        if ($LASTEXITCODE -ne 0) {
            throw "Loading the application image into Kind failed."
        }
    }

    Invoke-Kubectl -n $Namespace delete job database-migrate --ignore-not-found=true
    Invoke-Kubectl apply -k deploy/k8s/overlays/local
    Invoke-Kubectl -n $Namespace rollout restart deployment/api deployment/worker
    Invoke-Kubectl -n $Namespace wait --for=condition=complete job/database-migrate --timeout=180s
    Reset-And-ApplyJob `
        -Name "demo-seed" `
        -Manifest "deploy/k8s/operations/demo-seed-job.yaml" `
        -Timeout "180s"
    Invoke-Kubectl -n $Namespace rollout status deployment/api --timeout=180s
    Invoke-Kubectl -n $Namespace rollout status deployment/worker --timeout=180s

    $health = Invoke-RestMethod -Uri "http://127.0.0.1:8080/health/ready" -TimeoutSec 15
    if ($health.status -ne "ok") {
        throw "Readiness endpoint returned '$($health.status)'."
    }

    $coverage = Invoke-RestMethod -Uri "http://127.0.0.1:8080/api/v1/coverage" -TimeoutSec 15
    $campaigns = Invoke-RestMethod -Uri "http://127.0.0.1:8080/api/v1/campaigns?limit=100" -TimeoutSec 15
    if ($coverage.Count -ne 10 -or @($campaigns.items).Count -ne 4) {
        throw "Demo smoke expected 10 coverage entries and 4 pending campaigns; found $($coverage.Count) and $(@($campaigns.items).Count)."
    }
    if (@($campaigns.items | Where-Object { $_.status -ne "needs_review" }).Count -ne 0) {
        throw "Demo smoke found a campaign that was incorrectly promoted beyond needs_review."
    }

    Invoke-Kubectl -n $Namespace exec deployment/ollama "--" ollama ps
    Write-Host "Katılım Analiz is ready at http://127.0.0.1:8080" -ForegroundColor Green
}
finally {
    Pop-Location
}
