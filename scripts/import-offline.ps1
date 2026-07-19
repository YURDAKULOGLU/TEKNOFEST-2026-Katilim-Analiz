[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$BundleDirectory,
    [switch]$ValidateOnly,
    [switch]$RequireNoOutboundNetwork
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ExpectedPayloadPaths = @(
    "images/kind-node.tar",
    "images/workloads.tar",
    "model/model.gguf",
    "model/Modelfile",
    "model/source.json"
)

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

function Invoke-NativeCapture {
    param(
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string[]]$Arguments,
        [Parameter(Mandatory)][string]$Description
    )

    $output = @(& $Executable @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed (exit $LASTEXITCODE): $($output -join [Environment]::NewLine)"
    }
    return ($output -join "`n").Trim()
}

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string[]]$Arguments,
        [Parameter(Mandatory)][string]$Description
    )

    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Resolve-ExistingBundleDirectory {
    param([Parameter(Mandatory)][string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "BundleDirectory cannot be empty."
    }
    $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $resolved -PathType Container)) {
        throw "BundleDirectory is not a directory: $resolved"
    }
    $bundleItem = Get-Item -LiteralPath $resolved -Force
    if (($bundleItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "BundleDirectory cannot itself be a symbolic link or reparse point: $resolved"
    }
    $pathRoot = [System.IO.Path]::GetPathRoot($resolved)
    if ($resolved.TrimEnd('\', '/') -eq $pathRoot.TrimEnd('\', '/')) {
        throw "BundleDirectory cannot be a filesystem root."
    }
    if ($resolved.TrimEnd('\', '/') -eq $RepoRoot.TrimEnd('\', '/')) {
        throw "BundleDirectory cannot be the repository root."
    }
    $reparsePoints = @(Get-ChildItem -LiteralPath $resolved -Force -Recurse |
        Where-Object { ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 })
    if ($reparsePoints.Count -gt 0) {
        throw "Bundle contains a symbolic link or reparse point: $($reparsePoints[0].FullName)"
    }
    return $resolved
}

function Resolve-BundleFile {
    param(
        [Parameter(Mandatory)][string]$BundleRoot,
        [Parameter(Mandatory)][string]$RelativePath
    )

    $segments = @($RelativePath -split '/')
    if ($RelativePath -notmatch '^[A-Za-z0-9._/-]+$' -or
        $RelativePath.Contains('\') -or
        $RelativePath.StartsWith('/') -or
        $RelativePath.EndsWith('/') -or
        $RelativePath.Contains('//') -or
        $segments -contains '.' -or
        $segments -contains '..') {
        throw "Bundle path is not canonical: $RelativePath"
    }
    $fullPath = [System.IO.Path]::GetFullPath((Join-Path $BundleRoot $RelativePath))
    $prefix = $BundleRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if (-not $fullPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Bundle path escapes the bundle directory: $RelativePath"
    }
    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
        throw "Bundle file is missing: $RelativePath"
    }
    return $fullPath
}

function Get-RequiredRepositoryPaths {
    $roots = @(
        "deploy/kind/cluster.yaml",
        "deploy/k8s/base",
        "deploy/k8s/components/network-boundary",
        "deploy/k8s/components/local-services",
        "deploy/k8s/overlays/local",
        "deploy/k8s/overlays/local-infra",
        "deploy/k8s/overlays/offline",
        "deploy/k8s/overlays/offline-infra",
        "deploy/k8s/overlays/offline-predeploy",
        "deploy/k8s/operations/demo-seed-job.yaml",
        "deploy/k8s/operations/model-warmup-job.yaml",
        "scripts/import-offline.ps1"
    )
    $files = foreach ($relativeRoot in $roots) {
        $absoluteRoot = Join-Path $RepoRoot $relativeRoot
        if (-not (Test-Path -LiteralPath $absoluteRoot)) {
            throw "Required repository input is missing: $relativeRoot"
        }
        if (Test-Path -LiteralPath $absoluteRoot -PathType Container) {
            Get-ChildItem -LiteralPath $absoluteRoot -File -Recurse -Filter "*.yaml"
        }
        else {
            Get-Item -LiteralPath $absoluteRoot
        }
    }
    return @($files | Sort-Object FullName -Unique | ForEach-Object {
        [System.IO.Path]::GetRelativePath($RepoRoot, $_.FullName).Replace('\', '/')
    })
}

function Read-JsonFile {
    param([Parameter(Mandatory)][string]$Path)

    try {
        return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -Depth 20
    }
    catch {
        throw "Invalid JSON in '$Path': $($_.Exception.Message)"
    }
}

function Assert-Sha256 {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Expected,
        [Parameter(Mandatory)][string]$Description
    )

    if ($Expected -notmatch '^[0-9a-f]{64}$') {
        throw "$Description has an invalid expected SHA-256 value."
    }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $Expected) {
        throw "$Description checksum mismatch: expected $Expected, received $actual."
    }
}

function Assert-BundleIntegrity {
    param([Parameter(Mandatory)][string]$BundleRoot)

    $manifestPath = Resolve-BundleFile -BundleRoot $BundleRoot -RelativePath "bundle-manifest.json"
    $sidecarPath = Resolve-BundleFile -BundleRoot $BundleRoot -RelativePath "bundle-manifest.sha256.json"
    $sidecar = Read-JsonFile -Path $sidecarPath
    if ($sidecar.algorithm -ne "SHA-256" -or $sidecar.file -ne "bundle-manifest.json") {
        throw "Manifest checksum sidecar has an unsupported format."
    }
    Assert-Sha256 -Path $manifestPath -Expected ([string]$sidecar.sha256) -Description "Bundle manifest"
    $manifest = Read-JsonFile -Path $manifestPath

    if ([int]$manifest.schemaVersion -ne 1 -or $manifest.bundleType -ne "katilim-analiz-offline-kind") {
        throw "Unsupported offline bundle schema or type."
    }
    if ($manifest.platform -notmatch '^linux/(amd64|arm64)$') {
        throw "Manifest image platform is missing or unsupported."
    }
    if ($manifest.cluster.name -notmatch '^[a-z0-9]([-a-z0-9]*[a-z0-9])?$') {
        throw "Manifest cluster name is not a valid DNS label."
    }
    if ($manifest.cluster.namespace -notmatch '^[a-z0-9]([-a-z0-9]*[a-z0-9])?$') {
        throw "Manifest namespace is not a valid DNS label."
    }
    if ($manifest.cluster.nodeImage -notmatch '^kindest/node:[^\s@]+@sha256:[0-9a-f]{64}$') {
        throw "Manifest Kind node image is not digest-pinned."
    }
    if ($manifest.application.image -match '(^|:)latest$' -or $manifest.application.image -notmatch ':[^/@]+(?:@sha256:[0-9a-f]{64})?$') {
        throw "Manifest application image must use an explicit non-latest tag."
    }
    if ($manifest.model.name -notmatch '^[A-Za-z0-9._/-]+:[A-Za-z0-9._-]+$') {
        throw "Manifest model name is not explicit and valid."
    }
    if ($manifest.model.digest -notmatch '^[0-9a-f]{64}$') {
        throw "Manifest model digest is invalid."
    }
    if ($manifest.model.sourceBlobDigest -notmatch '^sha256:[0-9a-f]{64}$') {
        throw "Manifest source model blob digest is invalid."
    }
    if ($manifest.model.artefact -ne "model/model.gguf" -or $manifest.model.modelfile -ne "model/Modelfile") {
        throw "Manifest model payload paths are not supported."
    }

    $records = @($manifest.files)
    if ($records.Count -ne $ExpectedPayloadPaths.Count) {
        throw "Manifest payload file count is invalid: $($records.Count)."
    }
    $recordPaths = @($records | ForEach-Object { [string]$_.path })
    if (@($recordPaths | Sort-Object -Unique).Count -ne $records.Count) {
        throw "Manifest contains duplicate payload paths."
    }
    foreach ($expectedPath in $ExpectedPayloadPaths) {
        if ($recordPaths -notcontains $expectedPath) {
            throw "Manifest is missing required payload: $expectedPath"
        }
    }
    $expectedKinds = @{
        "images/kind-node.tar" = "kind-node-image-archive"
        "images/workloads.tar" = "workload-image-archive"
        "model/model.gguf" = "ollama-gguf"
        "model/Modelfile" = "ollama-modelfile"
        "model/source.json" = "ollama-source-metadata"
    }
    foreach ($record in $records) {
        if ($expectedKinds[[string]$record.path] -ne [string]$record.kind) {
            throw "Payload kind is invalid for '$($record.path)'."
        }
        $payloadPath = Resolve-BundleFile -BundleRoot $BundleRoot -RelativePath ([string]$record.path)
        $payloadFile = Get-Item -LiteralPath $payloadPath
        if ([long]$record.size -ne [long]$payloadFile.Length) {
            throw "Payload size mismatch for '$($record.path)'."
        }
        Assert-Sha256 -Path $payloadPath -Expected ([string]$record.sha256) -Description "Payload '$($record.path)'"
    }

    $allBundleFiles = @(Get-ChildItem -LiteralPath $BundleRoot -File -Force -Recurse |
        ForEach-Object { [System.IO.Path]::GetRelativePath($BundleRoot, $_.FullName).Replace('\', '/') } |
        Sort-Object)
    $expectedAllFiles = @($ExpectedPayloadPaths + @("bundle-manifest.json", "bundle-manifest.sha256.json") | Sort-Object)
    if (($allBundleFiles -join "`n") -ne ($expectedAllFiles -join "`n")) {
        throw "Bundle contains missing or unexpected files. Expected: $($expectedAllFiles -join ', '); found: $($allBundleFiles -join ', ')."
    }

    $ggufPath = Resolve-BundleFile -BundleRoot $BundleRoot -RelativePath "model/model.gguf"
    $stream = [System.IO.File]::OpenRead($ggufPath)
    try {
        $magic = [byte[]]::new(4)
        if ($stream.Read($magic, 0, 4) -ne 4 -or [System.Text.Encoding]::ASCII.GetString($magic) -ne "GGUF") {
            throw "model/model.gguf does not start with the GGUF magic bytes."
        }
    }
    finally {
        $stream.Dispose()
    }

    $sourceMetadataPath = Resolve-BundleFile -BundleRoot $BundleRoot -RelativePath "model/source.json"
    $sourceMetadata = Read-JsonFile -Path $sourceMetadataPath
    if ($sourceMetadata.model -ne $manifest.model.name -or $sourceMetadata.modelDigest -ne $manifest.model.digest) {
        throw "Model source metadata disagrees with the bundle manifest."
    }
    if ($sourceMetadata.sourceBlobDigest -ne $manifest.model.sourceBlobDigest) {
        throw "Model source-blob metadata disagrees with the bundle manifest."
    }
    $ggufRecord = @($records | Where-Object { $_.path -eq "model/model.gguf" })[0]
    if ("sha256:$($ggufRecord.sha256)" -ne $manifest.model.sourceBlobDigest) {
        throw "GGUF file checksum does not equal the source Ollama blob digest."
    }

    $modelfilePath = Resolve-BundleFile -BundleRoot $BundleRoot -RelativePath "model/Modelfile"
    $modelfile = Get-Content -LiteralPath $modelfilePath -Raw
    $portableFrom = [regex]::Matches($modelfile, '(?m)^FROM\s+\./model\.gguf\s*$')
    $allFrom = [regex]::Matches($modelfile, '(?m)^FROM\s+\S+\s*$')
    if ($portableFrom.Count -ne 1 -or $allFrom.Count -ne 1) {
        throw "Portable Modelfile must contain exactly one 'FROM ./model.gguf' instruction."
    }

    $repositoryInputs = @($manifest.repositoryInputs)
    if ($repositoryInputs.Count -eq 0) {
        throw "Manifest has no repository-input checksums."
    }
    $repositoryPaths = @($repositoryInputs | ForEach-Object { [string]$_.path })
    if (@($repositoryPaths | Sort-Object -Unique).Count -ne $repositoryInputs.Count) {
        throw "Manifest contains duplicate repository-input paths."
    }
    $requiredRepositoryPaths = @(Get-RequiredRepositoryPaths | Sort-Object)
    if ((@($repositoryPaths | Sort-Object) -join "`n") -ne ($requiredRepositoryPaths -join "`n")) {
        throw "Manifest repository-input set is incomplete or unexpected."
    }
    foreach ($input in $repositoryInputs) {
        $relativePath = [string]$input.path
        $segments = @($relativePath -split '/')
        if ($relativePath -notmatch '^[A-Za-z0-9._/-]+$' -or
            $relativePath.Contains('\') -or
            $relativePath.StartsWith('/') -or
            $relativePath.EndsWith('/') -or
            $relativePath.Contains('//') -or
            $segments -contains '.' -or
            $segments -contains '..') {
            throw "Repository input path is not canonical: $relativePath"
        }
        $inputPath = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $relativePath))
        $repoPrefix = $RepoRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
        if (-not $inputPath.StartsWith($repoPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Repository input escapes the repository: $relativePath"
        }
        if (-not (Test-Path -LiteralPath $inputPath -PathType Leaf)) {
            throw "Repository input is missing: $relativePath"
        }
        $inputFile = Get-Item -LiteralPath $inputPath
        if ([long]$input.size -ne [long]$inputFile.Length) {
            throw "Repository input size differs from the exported release: $relativePath"
        }
        Assert-Sha256 -Path $inputPath -Expected ([string]$input.sha256) -Description "Repository input '$relativePath'"
    }

    $kindConfig = Get-Content -LiteralPath (Join-Path $RepoRoot "deploy/kind/cluster.yaml") -Raw
    $configuredClusterNames = [regex]::Matches(
        $kindConfig,
        '(?m)^name:\s*(?<name>[a-z0-9]([-a-z0-9]*[a-z0-9])?)\s*$'
    )
    $configuredNodeImages = [regex]::Matches(
        $kindConfig,
        '(?m)^\s*image:\s*(?<image>kindest/node:[^\s#]+)\s*$'
    )
    if ($configuredClusterNames.Count -ne 1 -or $configuredClusterNames[0].Groups['name'].Value -ne $manifest.cluster.name) {
        throw "Manifest cluster name differs from deploy/kind/cluster.yaml."
    }
    if ($configuredNodeImages.Count -ne 1 -or $configuredNodeImages[0].Groups['image'].Value -ne $manifest.cluster.nodeImage) {
        throw "Manifest node image differs from deploy/kind/cluster.yaml."
    }
    $offlineKustomization = Get-Content -LiteralPath (
        Join-Path $RepoRoot "deploy/k8s/overlays/offline/kustomization.yaml"
    ) -Raw
    $configuredNamespaces = [regex]::Matches(
        $offlineKustomization,
        '(?m)^namespace:\s*(?<namespace>[a-z0-9]([-a-z0-9]*[a-z0-9])?)\s*$'
    )
    if ($configuredNamespaces.Count -ne 1 -or $configuredNamespaces[0].Groups['namespace'].Value -ne $manifest.cluster.namespace) {
        throw "Manifest namespace differs from the offline Kustomization."
    }
    $baseConfig = Get-Content -LiteralPath (Join-Path $RepoRoot "deploy/k8s/base/config-map.yaml") -Raw
    $configuredModel = [regex]::Match(
        $baseConfig,
        '(?m)^\s*OLLAMA_MODEL:\s*["'']?(?<model>[A-Za-z0-9._/-]+:[A-Za-z0-9._-]+)["'']?\s*$'
    )
    $configuredModelDigest = [regex]::Match(
        $baseConfig,
        '(?m)^\s*OLLAMA_MODEL_DIGEST:\s*["'']?(?<digest>[0-9a-f]{64})["'']?\s*$'
    )
    if (-not $configuredModel.Success -or $configuredModel.Groups['model'].Value -ne $manifest.model.name) {
        throw "Manifest model differs from the application ConfigMap."
    }
    if (-not $configuredModelDigest.Success -or $configuredModelDigest.Groups['digest'].Value -ne $manifest.model.digest) {
        throw "Manifest model digest differs from the application ConfigMap."
    }
    $applicationManifests = @(
        "deploy/k8s/base/api-deployment.yaml",
        "deploy/k8s/base/worker-deployment.yaml",
        "deploy/k8s/base/migration-job.yaml"
    )
    $configuredApplicationImages = @($applicationManifests | ForEach-Object {
        $content = Get-Content -LiteralPath (Join-Path $RepoRoot $_) -Raw
        [regex]::Matches($content, '(?m)^\s*image:\s*(?<image>ghcr\.io/[^\s#]+)\s*$') |
            ForEach-Object { $_.Groups['image'].Value }
    } | Sort-Object -Unique)
    if ($configuredApplicationImages.Count -ne 1 -or $configuredApplicationImages[0] -ne $manifest.application.image) {
        throw "Manifest application image differs from the Kubernetes application manifests."
    }

    $workloadImages = @($manifest.application.workloadImages)
    if ($workloadImages.Count -lt 4) {
        throw "Manifest contains fewer workload images than the offline profile requires."
    }
    if (@($workloadImages | Where-Object { $_.reference -eq $manifest.application.image }).Count -ne 1) {
        throw "Application image is not represented exactly once in workloadImages."
    }
    foreach ($image in $workloadImages) {
        if ($image.reference -ne $manifest.application.image -and $image.reference -notmatch '@sha256:[0-9a-f]{64}$') {
            throw "Runtime image is not digest-pinned: $($image.reference)"
        }
        if ($image.imageId -notmatch '^sha256:[0-9a-f]{64}$') {
            throw "Runtime image ID is invalid for $($image.reference)."
        }
        if ($image.platform -ne $manifest.platform) {
            throw "Runtime image platform differs from the bundle platform for $($image.reference)."
        }
    }

    return $manifest
}

function Invoke-KubectlChecked {
    param(
        [Parameter(Mandatory)][string]$KubectlExe,
        [Parameter(Mandatory)][string]$Context,
        [Parameter(Mandatory)][string[]]$Arguments,
        [Parameter(Mandatory)][string]$Description
    )

    Invoke-NativeChecked -Executable $KubectlExe -Arguments (@("--context", $Context) + $Arguments) -Description $Description
}

function Protect-ExistingRuntimeForOfflineImport {
    param(
        [Parameter(Mandatory)][string]$KubectlExe,
        [Parameter(Mandatory)][string]$Context,
        [Parameter(Mandatory)][string]$Namespace
    )

    $existingNamespace = Invoke-NativeCapture -Executable $KubectlExe `
        -Arguments @(
            "--context", $Context,
            "get", "namespace", $Namespace,
            "--ignore-not-found=true", "-o", "name"
        ) `
        -Description "Checking for an existing application namespace"
    if ([string]::IsNullOrWhiteSpace($existingNamespace)) {
        return
    }

    # Establish deny-by-default before removing the connected profile's explicit
    # HTTPS allowance. This prevents a running model/worker from gaining a brief
    # unrestricted interval while an existing cluster is being converted.
    Invoke-KubectlChecked -KubectlExe $KubectlExe -Context $Context `
        -Arguments @(
            "-n", $Namespace,
            "apply", "-f", "deploy/k8s/components/network-boundary/network-policies.yaml"
        ) `
        -Description "Establishing the offline network boundary"
    Invoke-KubectlChecked -KubectlExe $KubectlExe -Context $Context `
        -Arguments @(
            "-n", $Namespace,
            "delete", "networkpolicy", "allow-approved-public-https",
            "--ignore-not-found=true"
        ) `
        -Description "Removing the connected public-HTTPS allowance"

    $existingConfig = Invoke-NativeCapture -Executable $KubectlExe `
        -Arguments @(
            "--context", $Context,
            "-n", $Namespace,
            "get", "configmap", "katilim-analiz-config",
            "--ignore-not-found=true", "-o", "name"
        ) `
        -Description "Checking for an existing application ConfigMap"
    if (-not [string]::IsNullOrWhiteSpace($existingConfig)) {
        Invoke-KubectlChecked -KubectlExe $KubectlExe -Context $Context `
            -Arguments @(
                "-n", $Namespace,
                "patch", "configmap", "katilim-analiz-config",
                "--type=merge",
                "-p", '{"data":{"APP_ENV":"offline-kubernetes","INGEST_NETWORK_ENABLED":"false"}}'
            ) `
            -Description "Disabling connected ingestion before offline preparation"
    }

    foreach ($role in @("api", "worker")) {
        $existingDeployment = Invoke-NativeCapture -Executable $KubectlExe `
            -Arguments @(
                "--context", $Context,
                "-n", $Namespace,
                "get", "deployment", $role,
                "--ignore-not-found=true", "-o", "name"
            ) `
            -Description "Checking for an existing $role deployment"
        if (-not [string]::IsNullOrWhiteSpace($existingDeployment)) {
            Invoke-KubectlChecked -KubectlExe $KubectlExe -Context $Context `
                -Arguments @("-n", $Namespace, "scale", "deployment/$role", "--replicas=0") `
                -Description "Quiescing the existing $role before offline preparation"
            Invoke-KubectlChecked -KubectlExe $KubectlExe -Context $Context `
                -Arguments @(
                    "-n", $Namespace,
                    "wait", "--for=delete", "pod",
                    "-l", "app.kubernetes.io/component=$role",
                    "--timeout=180s"
                ) `
                -Description "Waiting for existing $role pods to stop"
        }
    }
}

function Get-OllamaModelInventory {
    param(
        [Parameter(Mandatory)][string]$KubectlExe,
        [Parameter(Mandatory)][string]$Context,
        [Parameter(Mandatory)][string]$Namespace
    )

    $json = Invoke-NativeCapture -Executable $KubectlExe `
        -Arguments @(
            "--context", $Context,
            "get", "--raw",
            "/api/v1/namespaces/$Namespace/services/http:ollama:11434/proxy/api/tags"
        ) `
        -Description "Reading the Ollama local-model inventory"
    return $json | ConvertFrom-Json
}

function Reset-And-ApplyJob {
    param(
        [Parameter(Mandatory)][string]$KubectlExe,
        [Parameter(Mandatory)][string]$Context,
        [Parameter(Mandatory)][string]$Namespace,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Manifest,
        [Parameter(Mandatory)][string]$Timeout
    )

    Invoke-KubectlChecked -KubectlExe $KubectlExe -Context $Context `
        -Arguments @("-n", $Namespace, "delete", "job", $Name, "--ignore-not-found=true") `
        -Description "Resetting Job $Name"
    Invoke-KubectlChecked -KubectlExe $KubectlExe -Context $Context `
        -Arguments @("-n", $Namespace, "apply", "-f", $Manifest) `
        -Description "Applying Job $Name"
    Invoke-KubectlChecked -KubectlExe $KubectlExe -Context $Context `
        -Arguments @("-n", $Namespace, "wait", "--for=condition=complete", "job/$Name", "--timeout=$Timeout") `
        -Description "Waiting for Job $Name"
    Invoke-KubectlChecked -KubectlExe $KubectlExe -Context $Context `
        -Arguments @("-n", $Namespace, "logs", "job/$Name") `
        -Description "Reading Job $Name logs"
}

$bundleRoot = Resolve-ExistingBundleDirectory -Path $BundleDirectory
$offlinePreparationStarted = $false
$fullRuntimeMayBeServing = $false
Push-Location $RepoRoot
try {
    $manifest = Assert-BundleIntegrity -BundleRoot $bundleRoot
    if ($ValidateOnly) {
        [ordered]@{
            status = "valid"
            bundle = $bundleRoot
            manifestSha256 = (Get-FileHash -LiteralPath (Join-Path $bundleRoot "bundle-manifest.json") -Algorithm SHA256).Hash.ToLowerInvariant()
            filesVerified = $ExpectedPayloadPaths.Count
            repositoryInputsVerified = @($manifest.repositoryInputs).Count
            clusterName = $manifest.cluster.name
            model = $manifest.model.name
            modelDigest = $manifest.model.digest
        } | ConvertTo-Json
        return
    }

    $DockerExe = Resolve-RequiredCommand -Name "docker"
    $KindExe = Resolve-RequiredCommand -Name "kind"
    $KubectlExe = Resolve-RequiredCommand -Name "kubectl"
    Invoke-NativeChecked -Executable $DockerExe -Arguments @("info") -Description "Checking Docker Engine"
    $dockerPlatform = Invoke-NativeCapture -Executable $DockerExe `
        -Arguments @("info", "--format", "{{.OSType}}/{{.Architecture}}") `
        -Description "Checking the Docker Engine platform"
    $dockerPlatform = switch ($dockerPlatform) {
        "linux/x86_64" { "linux/amd64" }
        "linux/aarch64" { "linux/arm64" }
        default { $dockerPlatform }
    }
    if ($dockerPlatform -ne $manifest.platform) {
        throw "Docker Engine platform '$dockerPlatform' differs from bundle platform '$($manifest.platform)'."
    }

    $clusterName = [string]$manifest.cluster.name
    $namespace = [string]$manifest.cluster.namespace
    $context = "kind-$clusterName"
    $clustersText = Invoke-NativeCapture -Executable $KindExe -Arguments @("get", "clusters") -Description "Listing Kind clusters"
    $clusters = @($clustersText -split "`n")
    $clusterExists = $clusters -contains $clusterName
    $nodes = @()
    if ($clusterExists) {
        $nodesText = Invoke-NativeCapture -Executable $KindExe `
            -Arguments @("get", "nodes", "--name", $clusterName) `
            -Description "Inspecting the existing Kind cluster"
        $nodes = @($nodesText -split "`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        if ($nodes.Count -ne 1) {
            throw "Cluster '$clusterName' is not the expected single-node profile."
        }
        $existingNodeImage = Invoke-NativeCapture -Executable $DockerExe `
            -Arguments @("inspect", $nodes[0], "--format", "{{.Config.Image}}") `
            -Description "Checking the existing Kind node image"
        if ($existingNodeImage -ne $manifest.cluster.nodeImage) {
            throw "Existing cluster uses '$existingNodeImage', not bundle node '$($manifest.cluster.nodeImage)'. Cluster deletion is never automatic."
        }

        $offlinePreparationStarted = $true
        Protect-ExistingRuntimeForOfflineImport -KubectlExe $KubectlExe -Context $context -Namespace $namespace
        Write-Warning "Existing runtime was placed in fail-closed offline preparation mode. If import now fails, its API/worker remain stopped and public HTTPS egress remains removed."
    }

    $nodeArchive = Resolve-BundleFile -BundleRoot $bundleRoot -RelativePath "images/kind-node.tar"
    $workloadArchive = Resolve-BundleFile -BundleRoot $bundleRoot -RelativePath "images/workloads.tar"
    Invoke-NativeChecked -Executable $DockerExe -Arguments @("image", "load", "--input", $nodeArchive) -Description "Loading the Kind node image archive"
    Invoke-NativeChecked -Executable $DockerExe -Arguments @("image", "load", "--input", $workloadArchive) -Description "Loading the workload image archive"

    $requiredImages = @($manifest.cluster.nodeImage) + @($manifest.application.workloadImages | ForEach-Object { $_.reference })
    foreach ($image in $requiredImages) {
        $loadedImageId = Invoke-NativeCapture -Executable $DockerExe `
            -Arguments @("image", "inspect", "--format", "{{.Id}}", $image) `
            -Description "Inspecting loaded image $image"
        $loadedImagePlatform = Invoke-NativeCapture -Executable $DockerExe `
            -Arguments @("image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", $image) `
            -Description "Inspecting the loaded image platform for $image"
        if ($loadedImagePlatform -ne $manifest.platform) {
            throw "Loaded image platform for '$image' differs from the bundle manifest."
        }
        $expectedRecord = @($manifest.application.workloadImages | Where-Object { $_.reference -eq $image })
        if ($expectedRecord.Count -eq 1 -and $loadedImageId -ne $expectedRecord[0].imageId) {
            throw "Loaded image ID for '$image' differs from the exported manifest."
        }
    }

    if (-not $clusterExists) {
        Invoke-NativeChecked -Executable $KindExe `
            -Arguments @("create", "cluster", "--config", "deploy/kind/cluster.yaml", "--wait", "180s") `
            -Description "Creating the offline Kind cluster"
        $nodesText = Invoke-NativeCapture -Executable $KindExe `
            -Arguments @("get", "nodes", "--name", $clusterName) `
            -Description "Inspecting the new Kind cluster"
        $nodes = @($nodesText -split "`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        if ($nodes.Count -ne 1) {
            throw "Cluster '$clusterName' is not the expected single-node profile."
        }
        $existingNodeImage = Invoke-NativeCapture -Executable $DockerExe `
            -Arguments @("inspect", $nodes[0], "--format", "{{.Config.Image}}") `
            -Description "Checking the new Kind node image"
        if ($existingNodeImage -ne $manifest.cluster.nodeImage) {
            throw "New cluster uses '$existingNodeImage', not bundle node '$($manifest.cluster.nodeImage)'. Cluster deletion is never automatic."
        }
    }

    Invoke-NativeChecked -Executable $KindExe `
        -Arguments @("load", "image-archive", $workloadArchive, "--name", $clusterName) `
        -Description "Loading workload images into Kind"
    foreach ($image in @($manifest.application.workloadImages)) {
        $nodeImageJson = Invoke-NativeCapture -Executable $DockerExe `
            -Arguments @("exec", $nodes[0], "crictl", "inspecti", "--output", "json", $image.reference) `
            -Description "Inspecting $($image.reference) in the Kind node runtime"
        $nodeImage = $nodeImageJson | ConvertFrom-Json
        if ($nodeImage.status.id -notmatch '^sha256:[0-9a-f]{64}$') {
            throw "Kind node runtime did not return a valid image ID for '$($image.reference)'."
        }
    }

    Invoke-KubectlChecked -KubectlExe $KubectlExe -Context $context `
        -Arguments @("apply", "-k", "deploy/k8s/overlays/offline-infra") `
        -Description "Applying fail-closed offline infrastructure"
    Invoke-KubectlChecked -KubectlExe $KubectlExe -Context $context `
        -Arguments @(
            "-n", $namespace,
            "delete", "networkpolicy", "allow-approved-public-https",
            "--ignore-not-found=true"
        ) `
        -Description "Ensuring the connected public-HTTPS allowance is absent"
    $publicEgressPolicy = Invoke-NativeCapture -Executable $KubectlExe `
        -Arguments @(
            "--context", $context,
            "-n", $namespace,
            "get", "networkpolicy", "allow-approved-public-https",
            "--ignore-not-found=true", "-o", "name"
        ) `
        -Description "Verifying the offline infrastructure network boundary"
    if (-not [string]::IsNullOrWhiteSpace($publicEgressPolicy)) {
        throw "Connected public-HTTPS NetworkPolicy is still present in the offline infrastructure phase."
    }
    Invoke-KubectlChecked -KubectlExe $KubectlExe -Context $context `
        -Arguments @("-n", $namespace, "rollout", "status", "statefulset/postgres", "--timeout=180s") `
        -Description "Waiting for PostgreSQL"
    Invoke-KubectlChecked -KubectlExe $KubectlExe -Context $context `
        -Arguments @("-n", $namespace, "rollout", "status", "deployment/ollama", "--timeout=180s") `
        -Description "Waiting for Ollama"

    $inventory = Get-OllamaModelInventory -KubectlExe $KubectlExe -Context $context -Namespace $namespace
    $existingModels = @($inventory.models | Where-Object { $_.name -eq $manifest.model.name })
    if ($existingModels.Count -gt 1) {
        throw "Ollama inventory contains duplicate entries for '$($manifest.model.name)'."
    }
    if ($existingModels.Count -eq 1 -and $existingModels[0].digest -ne $manifest.model.digest) {
        throw "PVC already contains '$($manifest.model.name)' with digest '$($existingModels[0].digest)'; refusing to overwrite it."
    }

    if ($existingModels.Count -eq 0) {
        $modelPod = Invoke-NativeCapture -Executable $KubectlExe `
            -Arguments @(
                "--context", $context,
                "-n", $namespace,
                "get", "pods",
                "-l", "app.kubernetes.io/component=model",
                "--field-selector=status.phase=Running",
                "-o", "jsonpath={.items[0].metadata.name}"
            ) `
            -Description "Locating the running Ollama pod"
        if ([string]::IsNullOrWhiteSpace($modelPod) -or $modelPod -match '\s') {
            throw "Exactly one running Ollama pod was not found."
        }
        Invoke-NativeCapture -Executable $KubectlExe `
            -Arguments @("--context", $context, "-n", $namespace, "exec", $modelPod, "--", "tar", "--version") `
            -Description "Checking the tar prerequisite in the Ollama container" | Out-Null
        $remoteImportDirectory = "/models/.offline-import-$([guid]::NewGuid().ToString('N'))"
        Invoke-KubectlChecked -KubectlExe $KubectlExe -Context $context `
            -Arguments @("-n", $namespace, "exec", $modelPod, "--", "mkdir", $remoteImportDirectory) `
            -Description "Creating the scoped model-import directory"
        try {
            $localModelDirectory = Join-Path $bundleRoot "model/."
            Invoke-KubectlChecked -KubectlExe $KubectlExe -Context $context `
                -Arguments @(
                    "-n", $namespace,
                    "cp", "--retries=3", "--no-preserve",
                    $localModelDirectory,
                    "$modelPod`:$remoteImportDirectory"
                ) `
                -Description "Copying the checksummed model artefact into the model PVC"
            Invoke-KubectlChecked -KubectlExe $KubectlExe -Context $context `
                -Arguments @(
                    "-n", $namespace,
                    "exec", $modelPod, "--",
                    "ollama", "create", $manifest.model.name,
                    "-f", "$remoteImportDirectory/Modelfile"
                ) `
                -Description "Creating the offline Ollama model"
        }
        finally {
            $cleanupFileArguments = @(
                "--context", $context,
                "-n", $namespace,
                "exec", $modelPod, "--",
                "rm", "-f",
                "$remoteImportDirectory/model.gguf",
                "$remoteImportDirectory/Modelfile",
                "$remoteImportDirectory/source.json"
            )
            & $KubectlExe @cleanupFileArguments
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "Could not remove all scoped model-import files at $remoteImportDirectory."
            }
            & $KubectlExe --context $context -n $namespace exec $modelPod -- rmdir $remoteImportDirectory
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "Could not remove scoped model-import directory $remoteImportDirectory."
            }
        }

        $inventory = Get-OllamaModelInventory -KubectlExe $KubectlExe -Context $context -Namespace $namespace
        $createdModels = @($inventory.models | Where-Object { $_.name -eq $manifest.model.name })
        if ($createdModels.Count -ne 1 -or $createdModels[0].digest -ne $manifest.model.digest) {
            $actualDigest = if ($createdModels.Count -eq 1) { $createdModels[0].digest } else { "missing-or-duplicate" }
            if ($createdModels.Count -eq 1) {
                & $KubectlExe --context $context -n $namespace exec $modelPod -- ollama rm $manifest.model.name
                if ($LASTEXITCODE -ne 0) {
                    throw "Seeded model digest '$actualDigest' differs from bundle digest '$($manifest.model.digest)', and automatic rollback failed."
                }
                Write-Warning "Removed the newly created model after its digest failed verification; the bundle artefact remains recoverable."
            }
            throw "Seeded model digest '$actualDigest' differs from bundle digest '$($manifest.model.digest)'."
        }
    }

    Reset-And-ApplyJob -KubectlExe $KubectlExe -Context $context -Namespace $namespace `
        -Name "model-warmup" -Manifest "deploy/k8s/operations/model-warmup-job.yaml" -Timeout "5m"

    Invoke-KubectlChecked -KubectlExe $KubectlExe -Context $context `
        -Arguments @("-n", $namespace, "delete", "job", "database-migrate", "--ignore-not-found=true") `
        -Description "Resetting the database migration Job"
    Invoke-KubectlChecked -KubectlExe $KubectlExe -Context $context `
        -Arguments @("apply", "-k", "deploy/k8s/overlays/offline-predeploy") `
        -Description "Applying the offline pre-migration runtime with API and worker stopped"
    foreach ($role in @("api", "worker")) {
        $predeployReplicas = Invoke-NativeCapture -Executable $KubectlExe `
            -Arguments @(
                "--context", $context,
                "-n", $namespace,
                "get", "deployment", $role,
                "-o", "jsonpath={.spec.replicas}"
            ) `
            -Description "Verifying the pre-migration $role replica count"
        if ($predeployReplicas -ne "0") {
            throw "Offline pre-migration deployment '$role' rendered $predeployReplicas replicas instead of zero."
        }
    }
    $offlineConfigJson = Invoke-NativeCapture -Executable $KubectlExe `
        -Arguments @(
            "--context", $context,
            "-n", $namespace,
            "get", "configmap", "katilim-analiz-config", "-o", "json"
        ) `
        -Description "Verifying the offline application configuration"
    $offlineConfig = $offlineConfigJson | ConvertFrom-Json
    if ($offlineConfig.data.APP_ENV -ne "offline-kubernetes" -or
        $offlineConfig.data.INGEST_NETWORK_ENABLED -ne "false") {
        throw "Offline runtime ConfigMap does not disable connected ingestion."
    }
    $publicEgressPolicy = Invoke-NativeCapture -Executable $KubectlExe `
        -Arguments @(
            "--context", $context,
            "-n", $namespace,
            "get", "networkpolicy", "allow-approved-public-https",
            "--ignore-not-found=true", "-o", "name"
        ) `
        -Description "Verifying the offline application network boundary"
    if (-not [string]::IsNullOrWhiteSpace($publicEgressPolicy)) {
        throw "Connected public-HTTPS NetworkPolicy is present after the offline runtime apply."
    }
    Invoke-KubectlChecked -KubectlExe $KubectlExe -Context $context `
        -Arguments @("-n", $namespace, "wait", "--for=condition=complete", "job/database-migrate", "--timeout=180s") `
        -Description "Waiting for the database migration Job"
    Write-Host "Seeding the evidence-backed demo snapshot after migration"
    Reset-And-ApplyJob -KubectlExe $KubectlExe -Context $context -Namespace $namespace `
        -Name "demo-seed" -Manifest "deploy/k8s/operations/demo-seed-job.yaml" -Timeout "180s"
    # Set before apply because a failed apply can still have persisted the
    # Deployment replica changes. The outer catch re-quiesces both roles.
    $fullRuntimeMayBeServing = $true
    Invoke-KubectlChecked -KubectlExe $KubectlExe -Context $context `
        -Arguments @("apply", "-k", "deploy/k8s/overlays/offline") `
        -Description "Starting the full offline runtime after database migration"
    $publicEgressPolicy = Invoke-NativeCapture -Executable $KubectlExe `
        -Arguments @(
            "--context", $context,
            "-n", $namespace,
            "get", "networkpolicy", "allow-approved-public-https",
            "--ignore-not-found=true", "-o", "name"
        ) `
        -Description "Rechecking the full offline application network boundary"
    if (-not [string]::IsNullOrWhiteSpace($publicEgressPolicy)) {
        throw "Connected public-HTTPS NetworkPolicy is present after the full offline runtime apply."
    }
    Invoke-KubectlChecked -KubectlExe $KubectlExe -Context $context `
        -Arguments @("-n", $namespace, "rollout", "status", "deployment/api", "--timeout=180s") `
        -Description "Waiting for the API rollout"
    Invoke-KubectlChecked -KubectlExe $KubectlExe -Context $context `
        -Arguments @("-n", $namespace, "rollout", "status", "deployment/worker", "--timeout=180s") `
        -Description "Waiting for the worker rollout"

    $health = Invoke-RestMethod -Uri "http://127.0.0.1:8080/health/ready" -TimeoutSec 15
    if ($health.status -ne "ok") {
        throw "Readiness endpoint returned '$($health.status)'."
    }

    $coverage = Invoke-RestMethod -Uri "http://127.0.0.1:8080/api/v1/coverage" -TimeoutSec 15
    $campaigns = Invoke-RestMethod -Uri "http://127.0.0.1:8080/api/v1/campaigns?limit=100" -TimeoutSec 15
    if ($coverage.Count -ne 10 -or @($campaigns.items).Count -ne 4) {
        throw "Offline demo smoke expected 10 coverage entries and 4 pending campaigns; found $($coverage.Count) and $(@($campaigns.items).Count)."
    }
    if (@($campaigns.items | Where-Object { $_.status -ne "needs_review" }).Count -ne 0) {
        throw "Offline demo smoke found a campaign incorrectly promoted beyond needs_review."
    }

    if ($RequireNoOutboundNetwork) {
        $negativeProbe = @'
import socket
import sys
s = socket.socket()
s.settimeout(5)
try:
    s.connect(("1.1.1.1", 443))
except OSError:
    sys.exit(0)
else:
    s.close()
    sys.exit(1)
'@
        Invoke-KubectlChecked -KubectlExe $KubectlExe -Context $context `
            -Arguments @(
                "-n", $namespace,
                "exec", "deployment/worker", "--",
                "python", "-c", $negativeProbe
            ) `
            -Description "Proving that the offline worker cannot reach public HTTPS"
    }
    else {
        Write-Warning "Outbound-network denial was not actively tested. Use -RequireNoOutboundNetwork in the EVAL-010 gate."
    }

    Invoke-KubectlChecked -KubectlExe $KubectlExe -Context $context `
        -Arguments @("-n", $namespace, "exec", "deployment/ollama", "--", "ollama", "ps") `
        -Description "Checking the warmed model"
    $offlinePreparationStarted = $false
    $fullRuntimeMayBeServing = $false
    Write-Host "Offline import completed without a model pull or cluster deletion." -ForegroundColor Green
    Write-Host "Katılım Analiz is ready at http://127.0.0.1:8080" -ForegroundColor Green
}
catch {
    $originalError = $_
    if ($offlinePreparationStarted -or $fullRuntimeMayBeServing) {
        try {
            Protect-ExistingRuntimeForOfflineImport `
                -KubectlExe $KubectlExe -Context $context -Namespace $namespace
            Write-Warning "Offline startup failed after application roles may have started; API/worker were returned to fail-closed zero-replica mode."
        }
        catch {
            Write-Warning "Offline startup failed and automatic API/worker re-quiescing also failed: $($_.Exception.Message)"
        }
    }
    throw $originalError
}
finally {
    Pop-Location
}
