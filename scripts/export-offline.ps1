[CmdletBinding()]
param(
    [string]$OutputDirectory,
    [string]$ClusterName = "katilim-analiz",
    [string]$Namespace = "katilim-analiz",
    [string]$AppImage = "ghcr.io/yurdakuloglu/katilim-analiz:dev",
    [string]$Model = "qwen3.5:4b",
    [switch]$UseCachedImages,
    [switch]$MetadataOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Context = "kind-$ClusterName"
$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)

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

function Resolve-NewDirectoryTarget {
    param([Parameter(Mandatory)][string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "-OutputDirectory is required unless -MetadataOnly is used."
    }
    $fullPath = [System.IO.Path]::GetFullPath($Path, (Get-Location).Path)
    $pathRoot = [System.IO.Path]::GetPathRoot($fullPath)
    if ($fullPath.TrimEnd('\', '/') -eq $pathRoot.TrimEnd('\', '/')) {
        throw "The output directory cannot be a filesystem root."
    }
    if ($fullPath.TrimEnd('\', '/') -eq $RepoRoot.TrimEnd('\', '/')) {
        throw "The output directory cannot be the repository root."
    }
    if (Test-Path -LiteralPath $fullPath) {
        throw "Output target already exists; refusing to overwrite it: $fullPath"
    }
    $parent = Split-Path -Parent $fullPath
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        throw "Output parent directory does not exist: $parent"
    }
    return $fullPath
}

function Get-RepositoryInputs {
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
        [ordered]@{
            path = [System.IO.Path]::GetRelativePath($RepoRoot, $_.FullName).Replace('\', '/')
            sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            size = [long]$_.Length
        }
    })
}

function Get-OverlayMetadata {
    param([Parameter(Mandatory)][string]$KubectlExe)

    $rendered = Invoke-NativeCapture -Executable $KubectlExe `
        -Arguments @("kustomize", "deploy/k8s/overlays/offline") `
        -Description "Rendering the offline Kubernetes overlay"
    $warmupManifest = Get-Content -LiteralPath (
        Join-Path $RepoRoot "deploy/k8s/operations/model-warmup-job.yaml"
    ) -Raw
    $images = @([regex]::Matches("$rendered`n$warmupManifest", '(?m)^\s*image:\s*(?<image>[^\s#]+)\s*$') |
        ForEach-Object { $_.Groups['image'].Value } |
        Sort-Object -Unique)
    if ($images.Count -lt 4) {
        throw "The offline overlay rendered fewer images than expected: $($images -join ', ')"
    }
    if ($images -notcontains $AppImage) {
        throw "The requested application image '$AppImage' is not used by the offline overlay."
    }
    foreach ($image in $images) {
        if ($image -eq $AppImage) {
            continue
        }
        if ($image -notmatch '@sha256:[0-9a-f]{64}$') {
            throw "Runtime image is not digest-pinned: $image"
        }
    }

    $nodeConfig = Get-Content -LiteralPath (Join-Path $RepoRoot "deploy/kind/cluster.yaml") -Raw
    $clusterNameMatches = [regex]::Matches(
        $nodeConfig,
        '(?m)^name:\s*(?<name>[a-z0-9]([-a-z0-9]*[a-z0-9])?)\s*$'
    )
    if ($clusterNameMatches.Count -ne 1) {
        throw "deploy/kind/cluster.yaml must contain exactly one cluster name."
    }
    $nodeMatches = [regex]::Matches($nodeConfig, '(?m)^\s*image:\s*(?<image>kindest/node:[^\s#]+)\s*$')
    if ($nodeMatches.Count -ne 1) {
        throw "deploy/kind/cluster.yaml must contain exactly one Kind node image."
    }
    $nodeImage = $nodeMatches[0].Groups['image'].Value
    if ($nodeImage -notmatch '@sha256:[0-9a-f]{64}$') {
        throw "Kind node image is not digest-pinned: $nodeImage"
    }

    $digestMatch = [regex]::Match(
        $rendered,
        '(?m)^\s*OLLAMA_MODEL_DIGEST:\s*["'']?(?<digest>[0-9a-f]{64})["'']?\s*$'
    )
    if (-not $digestMatch.Success) {
        throw "OLLAMA_MODEL_DIGEST was not found in the rendered offline overlay."
    }
    $modelMatch = [regex]::Match(
        $rendered,
        '(?m)^\s*OLLAMA_MODEL:\s*["'']?(?<model>[A-Za-z0-9._/-]+:[A-Za-z0-9._-]+)["'']?\s*$'
    )
    if (-not $modelMatch.Success) {
        throw "OLLAMA_MODEL was not found in the rendered offline overlay."
    }
    $offlineKustomization = Get-Content -LiteralPath (
        Join-Path $RepoRoot "deploy/k8s/overlays/offline/kustomization.yaml"
    ) -Raw
    $namespaceMatches = [regex]::Matches(
        $offlineKustomization,
        '(?m)^namespace:\s*(?<namespace>[a-z0-9]([-a-z0-9]*[a-z0-9])?)\s*$'
    )
    if ($namespaceMatches.Count -ne 1) {
        throw "The offline Kustomization must contain exactly one namespace."
    }
    if ($rendered -notmatch '(?m)^\s*APP_ENV:\s*offline-kubernetes\s*$' -or
        $rendered -notmatch '(?m)^\s*INGEST_NETWORK_ENABLED:\s*["'']?false["'']?\s*$') {
        throw "The offline overlay must render APP_ENV=offline-kubernetes and INGEST_NETWORK_ENABLED=false."
    }
    if ($rendered -match '(?m)^\s*name:\s*allow-approved-public-https\s*$') {
        throw "The offline overlay must not render the connected public-HTTPS NetworkPolicy."
    }

    return [ordered]@{
        configuredClusterName = $clusterNameMatches[0].Groups['name'].Value
        configuredNamespace = $namespaceMatches[0].Groups['namespace'].Value
        nodeImage = $nodeImage
        workloadImages = $images
        configuredModelName = $modelMatch.Groups['model'].Value
        configuredModelDigest = $digestMatch.Groups['digest'].Value
    }
}

function Get-FileRecord {
    param(
        [Parameter(Mandatory)][string]$StagingDirectory,
        [Parameter(Mandatory)][System.IO.FileInfo]$File
    )

    $relativePath = [System.IO.Path]::GetRelativePath($StagingDirectory, $File.FullName).Replace('\', '/')
    $kind = switch -Wildcard ($relativePath) {
        "images/kind-node.tar" { "kind-node-image-archive"; break }
        "images/workloads.tar" { "workload-image-archive"; break }
        "model/model.gguf" { "ollama-gguf"; break }
        "model/Modelfile" { "ollama-modelfile"; break }
        "model/source.json" { "ollama-source-metadata"; break }
        default { throw "Unexpected payload file while building manifest: $relativePath" }
    }
    return [ordered]@{
        path = $relativePath
        kind = $kind
        sha256 = (Get-FileHash -LiteralPath $File.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        size = [long]$File.Length
    }
}

if ($ClusterName -notmatch '^[a-z0-9]([-a-z0-9]*[a-z0-9])?$') {
    throw "ClusterName is not a valid DNS label: $ClusterName"
}
if ($Namespace -notmatch '^[a-z0-9]([-a-z0-9]*[a-z0-9])?$') {
    throw "Namespace is not a valid DNS label: $Namespace"
}
if ($Model -notmatch '^[A-Za-z0-9._/-]+:[A-Za-z0-9._-]+$') {
    throw "Model must be an explicit Ollama name and tag, for example qwen3.5:4b."
}
if ($AppImage -match '(^|:)latest$' -or $AppImage -notmatch ':[^/@]+(?:@sha256:[0-9a-f]{64})?$') {
    throw "AppImage must use an explicit non-latest tag."
}

Push-Location $RepoRoot
try {
    $KubectlExe = Resolve-RequiredCommand -Name "kubectl"
    $overlay = Get-OverlayMetadata -KubectlExe $KubectlExe
    if ($ClusterName -ne $overlay.configuredClusterName) {
        throw "Requested cluster '$ClusterName' differs from deploy/kind/cluster.yaml name '$($overlay.configuredClusterName)'."
    }
    if ($Namespace -ne $overlay.configuredNamespace) {
        throw "Requested namespace '$Namespace' differs from the offline overlay namespace '$($overlay.configuredNamespace)'."
    }
    if ($Model -ne $overlay.configuredModelName) {
        throw "Requested model '$Model' differs from offline-overlay OLLAMA_MODEL '$($overlay.configuredModelName)'."
    }
    $repositoryInputs = Get-RepositoryInputs

    $plan = [ordered]@{
        schemaVersion = 1
        bundleType = "katilim-analiz-offline-kind"
        clusterName = $ClusterName
        namespace = $Namespace
        nodeImage = $overlay.nodeImage
        workloadImages = $overlay.workloadImages
        appImage = $AppImage
        model = $Model
        configuredModelDigest = $overlay.configuredModelDigest
        platform = "resolved-from-image-config-during-export"
        payload = @(
            "images/kind-node.tar",
            "images/workloads.tar",
            "model/model.gguf",
            "model/Modelfile",
            "model/source.json"
        )
        excluded = @("credentials", "Kubernetes Secrets", "PostgreSQL data", "raw third-party HTML")
        officialReferences = @(
            "https://kind.sigs.k8s.io/docs/user/working-offline/",
            "https://kind.sigs.k8s.io/docs/user/quick-start/#loading-an-image-into-your-cluster",
            "https://docs.ollama.com/modelfile",
            "https://docs.ollama.com/import"
        )
        repositoryInputs = $repositoryInputs
    }
    if ($MetadataOnly) {
        $plan | ConvertTo-Json -Depth 10
        return
    }

    $target = Resolve-NewDirectoryTarget -Path $OutputDirectory
    $targetParent = Split-Path -Parent $target
    $targetLeaf = Split-Path -Leaf $target
    $staging = Join-Path $targetParent ".$targetLeaf.staging-$([guid]::NewGuid().ToString('N'))"
    if (Test-Path -LiteralPath $staging) {
        throw "Unexpected staging-path collision: $staging"
    }

    $DockerExe = Resolve-RequiredCommand -Name "docker"
    $KindExe = Resolve-RequiredCommand -Name "kind"
    Invoke-NativeChecked -Executable $DockerExe -Arguments @("info") -Description "Checking Docker Engine"

    $completed = $false
    try {
        New-Item -ItemType Directory -Path (Join-Path $staging "images") -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $staging "model") -Force | Out-Null
        $externalImages = @($overlay.nodeImage) + @($overlay.workloadImages | Where-Object { $_ -ne $AppImage })
        if (-not $UseCachedImages) {
            foreach ($image in $externalImages) {
                Invoke-NativeChecked -Executable $DockerExe `
                    -Arguments @("image", "pull", $image) `
                    -Description "Pulling pinned image $image"
            }
        }

        $allImages = @($overlay.nodeImage) + @($overlay.workloadImages)
        $imageRecords = foreach ($image in $allImages) {
            $imageId = Invoke-NativeCapture -Executable $DockerExe `
                -Arguments @("image", "inspect", "--format", "{{.Id}}", $image) `
                -Description "Inspecting required image $image"
            $imagePlatform = Invoke-NativeCapture -Executable $DockerExe `
                -Arguments @("image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", $image) `
                -Description "Inspecting the platform of required image $image"
            [ordered]@{ reference = $image; imageId = $imageId; platform = $imagePlatform }
        }
        $platforms = @($imageRecords | ForEach-Object { $_.platform } | Sort-Object -Unique)
        if ($platforms.Count -ne 1 -or $platforms[0] -notmatch '^linux/(amd64|arm64)$') {
            throw "All offline images must target one supported Linux platform; found $($platforms -join ', ')."
        }

        Invoke-NativeChecked -Executable $DockerExe `
            -Arguments @("image", "save", "--output", (Join-Path $staging "images/kind-node.tar"), $overlay.nodeImage) `
            -Description "Saving the Kind node image"
        $workloadSaveArguments = @("image", "save", "--output", (Join-Path $staging "images/workloads.tar")) + @($overlay.workloadImages)
        Invoke-NativeChecked -Executable $DockerExe `
            -Arguments $workloadSaveArguments `
            -Description "Saving workload images"

        $clusters = @((Invoke-NativeCapture -Executable $KindExe -Arguments @("get", "clusters") -Description "Listing Kind clusters") -split "`n")
        if ($clusters -notcontains $ClusterName) {
            throw "Kind cluster '$ClusterName' is not running; the prepared model PVC cannot be exported."
        }

        $modelPod = Invoke-NativeCapture -Executable $KubectlExe `
            -Arguments @(
                "--context", $Context,
                "-n", $Namespace,
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
            -Arguments @("--context", $Context, "-n", $Namespace, "exec", $modelPod, "--", "tar", "--version") `
            -Description "Checking the tar prerequisite in the Ollama container" | Out-Null

        $tagsJson = Invoke-NativeCapture -Executable $KubectlExe `
            -Arguments @(
                "--context", $Context,
                "get", "--raw",
                "/api/v1/namespaces/$Namespace/services/http:ollama:11434/proxy/api/tags"
            ) `
            -Description "Reading the Ollama local-model inventory"
        $tags = $tagsJson | ConvertFrom-Json
        $modelEntries = @($tags.models | Where-Object { $_.name -eq $Model })
        if ($modelEntries.Count -ne 1) {
            throw "Model '$Model' is not present exactly once in Ollama's local inventory."
        }
        $modelEntry = $modelEntries[0]
        if ($modelEntry.digest -notmatch '^[0-9a-f]{64}$') {
            throw "Ollama returned an invalid digest for '$Model'."
        }
        if ($modelEntry.digest -ne $overlay.configuredModelDigest) {
            throw "Running model digest '$($modelEntry.digest)' differs from OLLAMA_MODEL_DIGEST '$($overlay.configuredModelDigest)'."
        }

        $sourceModelfile = Invoke-NativeCapture -Executable $KubectlExe `
            -Arguments @("--context", $Context, "-n", $Namespace, "exec", $modelPod, "--", "ollama", "show", $Model, "--modelfile") `
            -Description "Exporting the Ollama Modelfile"
        $fromPattern = [regex]::new('(?m)^FROM\s+(?<path>/models/blobs/sha256-(?<digest>[0-9a-f]{64}))\s*$')
        $fromMatches = $fromPattern.Matches($sourceModelfile)
        if ($fromMatches.Count -ne 1) {
            throw "The generated Modelfile did not contain exactly one local GGUF blob path."
        }
        $blobPath = $fromMatches[0].Groups['path'].Value
        $blobDigest = $fromMatches[0].Groups['digest'].Value
        $modelDirectory = Join-Path $staging "model"
        $ggufPath = Join-Path $modelDirectory "model.gguf"
        Invoke-NativeChecked -Executable $KubectlExe `
            -Arguments @("--context", $Context, "-n", $Namespace, "cp", "$modelPod`:$blobPath", $ggufPath) `
            -Description "Copying the GGUF model artefact from the model PVC"
        $ggufHash = (Get-FileHash -LiteralPath $ggufPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($ggufHash -ne $blobDigest) {
            throw "Copied GGUF checksum '$ggufHash' differs from Ollama blob digest '$blobDigest'."
        }

        $portableModelfile = $fromPattern.Replace($sourceModelfile, "FROM ./model.gguf", 1)
        [System.IO.File]::WriteAllText((Join-Path $modelDirectory "Modelfile"), "$portableModelfile`n", $Utf8NoBom)
        $sourceMetadata = [ordered]@{
            model = $Model
            modelDigest = $modelEntry.digest
            sourceBlobDigest = "sha256:$blobDigest"
            size = [long]$modelEntry.size
            details = $modelEntry.details
            capabilities = @($modelEntry.capabilities)
            ollamaImage = @($overlay.workloadImages | Where-Object { $_ -like "ollama/ollama:*" })[0]
            exportMethod = "ollama show --modelfile; portable GGUF FROM; ollama create"
        }
        [System.IO.File]::WriteAllText(
            (Join-Path $modelDirectory "source.json"),
            (($sourceMetadata | ConvertTo-Json -Depth 10) + "`n"),
            $Utf8NoBom
        )

        $payloadFiles = @(Get-ChildItem -LiteralPath $staging -File -Recurse | Sort-Object FullName)
        $fileRecords = @($payloadFiles | ForEach-Object { Get-FileRecord -StagingDirectory $staging -File $_ })
        if ($fileRecords.Count -ne 5) {
            throw "Offline payload must contain exactly five files; found $($fileRecords.Count)."
        }

        $manifest = [ordered]@{
            schemaVersion = 1
            bundleType = "katilim-analiz-offline-kind"
            createdAtUtc = [DateTimeOffset]::UtcNow.ToString("o")
            platform = $platforms[0]
            pointers = @("REQ-011", "REQ-013", "ADR-008", "EVAL-010", "EVAL-014")
            cluster = [ordered]@{
                name = $ClusterName
                namespace = $Namespace
                nodeImage = $overlay.nodeImage
            }
            application = [ordered]@{
                image = $AppImage
                workloadImages = $imageRecords | Where-Object { $_.reference -ne $overlay.nodeImage }
            }
            model = [ordered]@{
                name = $Model
                digest = $modelEntry.digest
                sourceBlobDigest = "sha256:$blobDigest"
                artefact = "model/model.gguf"
                modelfile = "model/Modelfile"
            }
            repositoryInputs = $repositoryInputs
            exclusions = @("credentials", "Kubernetes Secrets", "PostgreSQL data", "raw third-party HTML")
            files = $fileRecords
        }
        $manifestPath = Join-Path $staging "bundle-manifest.json"
        [System.IO.File]::WriteAllText($manifestPath, (($manifest | ConvertTo-Json -Depth 10) + "`n"), $Utf8NoBom)
        $manifestDigest = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
        $manifestChecksum = [ordered]@{
            algorithm = "SHA-256"
            file = "bundle-manifest.json"
            sha256 = $manifestDigest
        }
        [System.IO.File]::WriteAllText(
            (Join-Path $staging "bundle-manifest.sha256.json"),
            (($manifestChecksum | ConvertTo-Json) + "`n"),
            $Utf8NoBom
        )

        Move-Item -LiteralPath $staging -Destination $target
        $completed = $true
        Write-Host "Offline bundle created atomically at $target" -ForegroundColor Green
        Write-Host "Manifest SHA-256: $manifestDigest"
    }
    finally {
        if (-not $completed -and (Test-Path -LiteralPath $staging)) {
            $expectedPrefix = (Join-Path $targetParent ".$targetLeaf.staging-")
            if (-not $staging.StartsWith($expectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Refusing to clean an unexpected staging path: $staging"
            }
            Remove-Item -LiteralPath $staging -Recurse -Force
        }
    }
}
finally {
    Pop-Location
}
