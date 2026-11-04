param([Parameter(Mandatory=$true)][string]$PdfPath, [Parameter(Mandatory=$true)][string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$PdfPath = $PdfPath.Replace('/', '\')
$OutputDirectory = $OutputDirectory.Replace('/', '\')
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.CreationCollisionOption, Windows.Storage, ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Pdf.PdfDocument, Windows.Data.Pdf, ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Pdf.PdfPageRenderOptions, Windows.Data.Pdf, ContentType=WindowsRuntime] | Out-Null

function Wait-WinRtResult($Operation, [Type]$ResultType) {
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and
        $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters().Count -eq 1
    } | Select-Object -First 1
    $task = $method.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    try { $task.Wait() } catch { Write-Output $task.Exception.InnerException; throw }
    return $task.Result
}
function Wait-WinRtAction($Operation) {
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and -not $_.IsGenericMethod -and
        $_.GetParameters().Count -eq 1
    } | Select-Object -First 1
    $task = $method.Invoke($null, @($Operation))
    $task.Wait()
}

$inputPdf = Wait-WinRtResult ([Windows.Storage.StorageFile]::GetFileFromPathAsync($PdfPath)) ([Windows.Storage.StorageFile])
$document = Wait-WinRtResult ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($inputPdf)) ([Windows.Data.Pdf.PdfDocument])
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$folder = Wait-WinRtResult ([Windows.Storage.StorageFolder]::GetFolderFromPathAsync($OutputDirectory)) ([Windows.Storage.StorageFolder])
for ($index = 0; $index -lt $document.PageCount; $index++) {
    $page = $document.GetPage($index)
    $file = Wait-WinRtResult ($folder.CreateFileAsync(('page-' + ($index + 1) + '.png'), [Windows.Storage.CreationCollisionOption]::ReplaceExisting)) ([Windows.Storage.StorageFile])
    $stream = Wait-WinRtResult ($file.OpenAsync([Windows.Storage.FileAccessMode]::ReadWrite)) ([Windows.Storage.Streams.IRandomAccessStream])
    try {
        $options = New-Object Windows.Data.Pdf.PdfPageRenderOptions
        $options.DestinationWidth = 1191
        Wait-WinRtAction ($page.RenderToStreamAsync($stream, $options))
        Write-Output $file.Path
    } finally {
        $stream.Dispose()
        $page.Dispose()
    }
}
