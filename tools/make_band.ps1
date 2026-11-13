param([string]$InPreviewPng, [string]$OutBandPng)
Add-Type -AssemblyName System.Drawing, System.IO.Compression

$comp = [System.Drawing.Bitmap]::new($InPreviewPng)
$crect = [System.Drawing.Rectangle]::new(0, 221, 960, 336)
$cbd = $comp.LockBits($crect, [System.Drawing.Imaging.ImageLockMode]::ReadOnly, [System.Drawing.Imaging.PixelFormat]::Format24bppRgb)
$cstride = $cbd.Stride
$cbytes = [byte[]]::new($cstride * 336)
[System.Runtime.InteropServices.Marshal]::Copy($cbd.Scan0, $cbytes, 0, $cbytes.Length)
$comp.UnlockBits($cbd)
$comp.Dispose()

# 336 rows x (1 filter byte + 240 data bytes)
$raw = [byte[]]::new(336 * 241)
for ($y = 0; $y -lt 336; $y++) {
    $ro = $y * $cstride
    $po = $y * 241 + 1
    for ($x = 0; $x -lt 960; $x++) {
        $i = $ro + $x * 3
        $b = [int]$cbytes[$i]; $gr = [int]$cbytes[$i+1]; $r = [int]$cbytes[$i+2]
        if ($r -ge 128 -and $gr -lt 128 -and $b -lt 128) {
            $idx = 2
        } elseif ($r -lt 128 -and $gr -lt 128 -and $b -lt 128) {
            $idx = 1
        } else {
            $idx = 0
        }
        $shift = 6 - (($x -band 3) * 2)
        $raw[$po + ($x -shr 2)] = $raw[$po + ($x -shr 2)] -bor ($idx -shl $shift)
    }
}

function Get-Crc32([byte[]]$buf) {
    $c = [int64]0xFFFFFFFFL
    foreach ($b in $buf) {
        $c = $c -bxor [int64]$b
        for ($k=0; $k -lt 8; $k++) {
            if (($c -band 1L) -ne 0) { $c = (($c -shr 1) -band 0x7FFFFFFFL) -bxor 0xEDB88320L } else { $c = ($c -shr 1) -band 0x7FFFFFFFL }
        }
    }
    return ($c -bxor 0xFFFFFFFFL)
}
function New-Chunk([string]$type, [byte[]]$data) {
    $tb = [System.Text.Encoding]::ASCII.GetBytes($type)
    $len = $data.Length
    $lb = [byte[]]((($len -shr 24) -band 255), (($len -shr 16) -band 255), (($len -shr 8) -band 255), ($len -band 255))
    $td = [byte[]]::new(4 + $len)
    [Array]::Copy($tb, 0, $td, 0, 4); [Array]::Copy($data, 0, $td, 4, $len)
    $crc = Get-Crc32 $td
    $cb = [byte[]]((($crc -shr 24) -band 255), (($crc -shr 16) -band 255), (($crc -shr 8) -band 255), ($crc -band 255))
    $out = [byte[]]::new(12 + $len)
    [Array]::Copy($lb, 0, $out, 0, 4); [Array]::Copy($td, 0, $out, 4, 4 + $len); [Array]::Copy($cb, 0, $out, 8 + $len, 4)
    return ,$out
}

$ms = [System.IO.MemoryStream]::new()
$ms.WriteByte(0x78); $ms.WriteByte(0x9C)
$ds = [System.IO.Compression.DeflateStream]::new($ms, [System.IO.Compression.CompressionMode]::Compress, $true)
$ds.Write($raw, 0, $raw.Length); $ds.Dispose()
$s1 = [int64]1; $s2 = [int64]0
foreach ($b in $raw) { $s1 = ($s1 + $b) % 65521; $s2 = ($s2 + $s1) % 65521 }
$adler = ($s2 -shl 16) -bor $s1
$ms.WriteByte(($adler -shr 24) -band 255); $ms.WriteByte(($adler -shr 16) -band 255)
$ms.WriteByte(($adler -shr 8) -band 255);  $ms.WriteByte($adler -band 255)
$idat = $ms.ToArray(); $ms.Dispose()

# 960 (0x03C0) x 336 (0x0150), 2-bit palette
$ihdr = [byte[]](0,0,3,0xC0, 0,0,1,0x50, 2, 3, 0, 0, 0)
$plte = [byte[]](255,255,255,  0,0,0,  255,0,0,  255,255,255)
$fs = [System.IO.File]::Create($OutBandPng)
$sig = [byte[]](137,80,78,71,13,10,26,10)
$fs.Write($sig, 0, 8)
foreach ($ch in @((New-Chunk 'IHDR' $ihdr), (New-Chunk 'PLTE' $plte), (New-Chunk 'IDAT' $idat), (New-Chunk 'IEND' ([byte[]]@())))) {
    $fs.Write($ch, 0, $ch.Length)
}
$fs.Close()
Write-Output "$OutBandPng $((Get-Item $OutBandPng).Length) bytes"
