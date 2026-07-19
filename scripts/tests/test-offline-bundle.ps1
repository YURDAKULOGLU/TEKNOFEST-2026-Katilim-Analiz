[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$ExportScript = Join-Path $RepoRoot "scripts/export-offline.ps1"
$ImportScript = Join-Path $RepoRoot "scripts/import-offline.ps1"
$PowerShellExe = (Get-Process -Id $PID).Path
$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) "katilim-analiz-offline-test-$([guid]::NewGuid().ToString('N'))"

function Write-Utf8Json {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)]$Value,
        [int]$Depth = 10
    )

    [System.IO.File]::WriteAllText(
        $Path,
        (($Value | ConvertTo-Json -Depth $Depth) + "`n"),
        $Utf8NoBom
    )
}

function New-FileRecord {
    param(
        [Parameter(Mandatory)][string]$BundleRoot,
        [Parameter(Mandatory)][string]$RelativePath,
        [Parameter(Mandatory)][string]$Kind
    )

    $path = Join-Path $BundleRoot $RelativePath
    $file = Get-Item -LiteralPath $path
    return [ordered]@{
        path = $RelativePath.Replace('\', '/')
        kind = $Kind
        sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        size = [long]$file.Length
    }
}

function Assert-ImportRejected {
    param(
        [Parameter(Mandatory)][string]$Case,
        [Parameter(Mandatory)][string]$ErrorPattern
    )

    $output = @(& $PowerShellExe -NoProfile -File $ImportScript -BundleDirectory $tempRoot -ValidateOnly 2>&1)
    if ($LASTEXITCODE -eq 0) {
        throw "Validate-only accepted the invalid '$Case' fixture."
    }
    if (($output -join "`n") -notmatch $ErrorPattern) {
        throw "Validate-only rejected '$Case' for an unexpected reason: $($output -join [Environment]::NewLine)"
    }
}

function Get-RenderedDeploymentReplicas {
    param(
        [Parameter(Mandatory)][string]$RenderedYaml,
        [Parameter(Mandatory)][string]$Name
    )

    $documents = @($RenderedYaml -split '(?m)^---\s*$')
    $matches = @($documents | Where-Object {
        $_ -match '(?m)^kind:\s*Deployment\s*$' -and
        $_ -match "(?m)^  name:\s*$([regex]::Escape($Name))\s*$"
    })
    if ($matches.Count -ne 1) {
        throw "Expected exactly one rendered Deployment named '$Name'; found $($matches.Count)."
    }
    $replicaMatch = [regex]::Match($matches[0], '(?m)^  replicas:\s*(?<count>\d+)\s*$')
    if (-not $replicaMatch.Success) {
        throw "Rendered Deployment '$Name' has no explicit replica count."
    }
    return [int]$replicaMatch.Groups['count'].Value
}

New-Item -ItemType Directory -Path (Join-Path $tempRoot "images") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $tempRoot "model") -Force | Out-Null
try {
    $planText = @(& $PowerShellExe -NoProfile -File $ExportScript -MetadataOnly 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Metadata-only export failed: $($planText -join [Environment]::NewLine)"
    }
    $plan = ($planText -join "`n") | ConvertFrom-Json
    $repositoryInputPaths = @($plan.repositoryInputs | ForEach-Object { [string]$_.path })
    if ($repositoryInputPaths -notcontains "deploy/k8s/overlays/offline-predeploy/kustomization.yaml") {
        throw "Metadata-only export omitted the offline pre-migration overlay checksum."
    }
    if ($repositoryInputPaths -notcontains "deploy/k8s/operations/demo-seed-job.yaml") {
        throw "Metadata-only export omitted the evidence-backed demo seed Job checksum."
    }

    $kubectlExe = (Get-Command kubectl -ErrorAction Stop).Source
    $predeployYaml = @(& $kubectlExe kustomize (Join-Path $RepoRoot "deploy/k8s/overlays/offline-predeploy") 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Offline pre-migration render failed: $($predeployYaml -join [Environment]::NewLine)"
    }
    $predeploy = $predeployYaml -join "`n"
    foreach ($role in @("api", "worker")) {
        if ((Get-RenderedDeploymentReplicas -RenderedYaml $predeploy -Name $role) -ne 0) {
            throw "Offline pre-migration overlay does not keep '$role' at zero replicas."
        }
    }
    if ($predeploy -notmatch '(?m)^kind:\s*Job\s*$' -or
        $predeploy -notmatch '(?m)^  name:\s*database-migrate\s*$') {
        throw "Offline pre-migration overlay does not render the database migration Job."
    }
    if ($predeploy -notmatch '(?m)^\s*INGEST_NETWORK_ENABLED:\s*"false"\s*$' -or
        $predeploy -match '(?m)^  name:\s*allow-approved-public-https\s*$') {
        throw "Offline pre-migration overlay is not fail-closed."
    }

    $offlineYaml = @(& $kubectlExe kustomize (Join-Path $RepoRoot "deploy/k8s/overlays/offline") 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Full offline render failed: $($offlineYaml -join [Environment]::NewLine)"
    }
    $offline = $offlineYaml -join "`n"
    foreach ($role in @("api", "worker")) {
        if ((Get-RenderedDeploymentReplicas -RenderedYaml $offline -Name $role) -ne 1) {
            throw "Full offline overlay does not start '$role' at one replica."
        }
    }

    $importSource = Get-Content -LiteralPath $ImportScript -Raw
    $flowMarkers = @(
        "Protect-ExistingRuntimeForOfflineImport -KubectlExe",
        "Loading the Kind node image archive",
        "Applying the offline pre-migration runtime with API and worker stopped",
        "Waiting for the database migration Job",
        "Seeding the evidence-backed demo snapshot after migration",
        "Starting the full offline runtime after database migration"
    )
    $previousIndex = -1
    foreach ($marker in $flowMarkers) {
        $markerIndex = $importSource.IndexOf($marker, [System.StringComparison]::Ordinal)
        if ($markerIndex -le $previousIndex) {
            throw "Offline import flow marker is missing or out of order: $marker"
        }
        $previousIndex = $markerIndex
    }

    [System.IO.File]::WriteAllBytes((Join-Path $tempRoot "images/kind-node.tar"), [byte[]](1, 2, 3, 4))
    [System.IO.File]::WriteAllBytes((Join-Path $tempRoot "images/workloads.tar"), [byte[]](5, 6, 7, 8))
    $ggufBytes = [System.Text.Encoding]::ASCII.GetBytes("GGUFfixture")
    [System.IO.File]::WriteAllBytes((Join-Path $tempRoot "model/model.gguf"), $ggufBytes)
    [System.IO.File]::WriteAllText(
        (Join-Path $tempRoot "model/Modelfile"),
        "FROM ./model.gguf`nPARAMETER temperature 0`n",
        $Utf8NoBom
    )
    $ggufDigest = (Get-FileHash -LiteralPath (Join-Path $tempRoot "model/model.gguf") -Algorithm SHA256).Hash.ToLowerInvariant()
    $modelDigest = [string]$plan.configuredModelDigest
    Write-Utf8Json -Path (Join-Path $tempRoot "model/source.json") -Value ([ordered]@{
        model = $plan.model
        modelDigest = $modelDigest
        sourceBlobDigest = "sha256:$ggufDigest"
        exportMethod = "test fixture"
    })

    $fileRecords = @(
        New-FileRecord -BundleRoot $tempRoot -RelativePath "images/kind-node.tar" -Kind "kind-node-image-archive"
        New-FileRecord -BundleRoot $tempRoot -RelativePath "images/workloads.tar" -Kind "workload-image-archive"
        New-FileRecord -BundleRoot $tempRoot -RelativePath "model/model.gguf" -Kind "ollama-gguf"
        New-FileRecord -BundleRoot $tempRoot -RelativePath "model/Modelfile" -Kind "ollama-modelfile"
        New-FileRecord -BundleRoot $tempRoot -RelativePath "model/source.json" -Kind "ollama-source-metadata"
    )
    $workloadRecords = @($plan.workloadImages | ForEach-Object {
        [ordered]@{
            reference = [string]$_
            imageId = "sha256:$('b' * 64)"
            platform = "linux/amd64"
        }
    })
    $manifest = [ordered]@{
        schemaVersion = 1
        bundleType = "katilim-analiz-offline-kind"
        createdAtUtc = [DateTimeOffset]::UtcNow.ToString("o")
        platform = "linux/amd64"
        pointers = @("EVAL-010", "EVAL-014")
        cluster = [ordered]@{
            name = $plan.clusterName
            namespace = $plan.namespace
            nodeImage = $plan.nodeImage
        }
        application = [ordered]@{
            image = $plan.appImage
            workloadImages = $workloadRecords
        }
        model = [ordered]@{
            name = $plan.model
            digest = $modelDigest
            sourceBlobDigest = "sha256:$ggufDigest"
            artefact = "model/model.gguf"
            modelfile = "model/Modelfile"
        }
        repositoryInputs = @($plan.repositoryInputs)
        exclusions = @("credentials", "Kubernetes Secrets", "PostgreSQL data", "raw third-party HTML")
        files = $fileRecords
    }
    $manifestPath = Join-Path $tempRoot "bundle-manifest.json"
    Write-Utf8Json -Path $manifestPath -Value $manifest
    $manifestDigest = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Utf8Json -Path (Join-Path $tempRoot "bundle-manifest.sha256.json") -Value ([ordered]@{
        algorithm = "SHA-256"
        file = "bundle-manifest.json"
        sha256 = $manifestDigest
    })

    $validationText = @(& $PowerShellExe -NoProfile -File $ImportScript -BundleDirectory $tempRoot -ValidateOnly 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Validate-only rejected a valid fixture: $($validationText -join [Environment]::NewLine)"
    }
    $validation = ($validationText -join "`n") | ConvertFrom-Json
    if ($validation.status -ne "valid" -or [int]$validation.filesVerified -ne 5) {
        throw "Validate-only returned an unexpected result."
    }

    $unexpectedPath = Join-Path $tempRoot "unexpected.txt"
    [System.IO.File]::WriteAllText($unexpectedPath, "must be rejected`n", $Utf8NoBom)
    Assert-ImportRejected -Case "unexpected file" -ErrorPattern "missing or unexpected files"
    Remove-Item -LiteralPath $unexpectedPath -Force

    $originalManifestBytes = [System.IO.File]::ReadAllBytes($manifestPath)
    $sidecarPath = Join-Path $tempRoot "bundle-manifest.sha256.json"
    $originalSidecarBytes = [System.IO.File]::ReadAllBytes($sidecarPath)
    $traversalManifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json -Depth 20
    $traversalManifest.files[0].path = "../escape"
    Write-Utf8Json -Path $manifestPath -Value $traversalManifest -Depth 20
    $traversalManifestDigest = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Utf8Json -Path $sidecarPath -Value ([ordered]@{
        algorithm = "SHA-256"
        file = "bundle-manifest.json"
        sha256 = $traversalManifestDigest
    })
    Assert-ImportRejected -Case "manifest path traversal" -ErrorPattern "missing required payload|not canonical"
    [System.IO.File]::WriteAllBytes($manifestPath, $originalManifestBytes)
    [System.IO.File]::WriteAllBytes($sidecarPath, $originalSidecarBytes)

    [System.IO.File]::AppendAllText((Join-Path $tempRoot "model/Modelfile"), "# corruption`n", $Utf8NoBom)
    Assert-ImportRejected -Case "corrupted payload" -ErrorPattern "Payload size mismatch|checksum mismatch"

    Write-Host "Offline bundle flow, metadata, integrity, path-safety, and corruption smoke tests: PASS" -ForegroundColor Green
}
finally {
    $tempBase = [System.IO.Path]::GetTempPath().TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if (-not $tempRoot.StartsWith($tempBase, [System.StringComparison]::OrdinalIgnoreCase) -or
        (Split-Path -Leaf $tempRoot) -notlike "katilim-analiz-offline-test-*") {
        throw "Refusing to clean unexpected test directory: $tempRoot"
    }
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force
    }
}
