param([string]$InJpg, [string]$BlankPng, [string]$OutFullPng, [string]$OutPreviewPng)
Add-Type -AssemblyName System.Drawing, System.IO.Compression

# Load the blank nametag (960x672) and the generated lettering JPEG
$blank = [System.Drawing.Bitmap]::new($BlankPng)
$src   = [System.Drawing.Bitmap]::new($InJpg)

# Lock bits on src to find non-white content bounding box
$rect = [System.Drawing.Rectangle]::new(0, 0, $src.Width, $src.Height)
$bd = $src.LockBits($rect, [System.Drawing.Imaging.ImageLockMode]::ReadOnly, [System.Drawing.Imaging.PixelFormat]::Format24bppRgb)
$stride = $bd.Stride
$bytes = [byte[]]::new($stride * $src.Height)
[System.Runtime.InteropServices.Marshal]::Copy($bd.Scan0, $bytes, 0, $bytes.Length)
$src.UnlockBits($bd)

$minX = $src.Width; $maxX = 0; $minY = $src.Height; $maxY = 0
for ($y = 0; $y -lt $src.Height; $y++) {
    $rowOff = $y * $stride
    for ($x = 0; $x -lt $src.Width; $x++) {
        $i = $rowOff + $x * 3
        $b = $bytes[$i]; $g = $bytes[$i+1]; $r = $bytes[$i+2]
        if ($r -lt 220 -or $g -lt 220 -or $b -lt 220) {
            if ($x -lt $minX) { $minX = $x }
            if ($x -gt $maxX) { $maxX = $x }
            if ($y -lt $minY) { $minY = $y }
            if ($y -gt $maxY) { $maxY = $y }
        }
    }
}
$cropW = $maxX - $minX + 1
$cropH = $maxY - $minY + 1

# Target band: rows 221..557 (height 336), width 960. Leave margin -> 900 x 296 max.
$scale = [Math]::Min(900.0 / $cropW, 296.0 / $cropH)
$dstW = [int][Math]::Round($cropW * $scale)
$dstH = [int][Math]::Round($cropH * $scale)
$dstX = (960 - $dstW) / 2
$dstY = 221 + (336 - $dstH) / 2

$comp = [System.Drawing.Bitmap]::new(960, 672, [System.Drawing.Imaging.PixelFormat]::Format24bppRgb)
$g = [System.Drawing.Graphics]::FromImage($comp)
$g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
$g.DrawImage($blank, 0, 0, 960, 672)
$g.DrawImage($src, [System.Drawing.Rectangle]::new($dstX, $dstY, $dstW, $dstH),
             [System.Drawing.Rectangle]::new($minX, $minY, $cropW, $cropH), [System.Drawing.GraphicsUnit]::Pixel)
$g.Dispose()

# Quantize every pixel strictly to palette: 0=white, 1=black, 2=red
$crect = [System.Drawing.Rectangle]::new(0, 0, 960, 672)
$cbd = $comp.LockBits($crect, [System.Drawing.Imaging.ImageLockMode]::ReadWrite, [System.Drawing.Imaging.PixelFormat]::Format24bppRgb)
$cstride = $cbd.Stride
$cbytes = [byte[]]::new($cstride * 672)
[System.Runtime.InteropServices.Marshal]::Copy($cbd.Scan0, $cbytes, 0, $cbytes.Length)

# 2-bit packed rows with filter byte 0: 1 + 960/4 = 241 bytes/row
$raw = [byte[]]::new(672 * 241)
for ($y = 0; $y -lt 672; $y++) {
    $ro = $y * $cstride
    $po = $y * 241 + 1   # byte 0 of each row is filter=0
    for ($x = 0; $x -lt 960; $x++) {
        $i = $ro + $x * 3
        $b = [int]$cbytes[$i]; $gr = [int]$cbytes[$i+1]; $r = [int]$cbytes[$i+2]
        # Classify: strong red excess -> red(2); dark -> black(1); else white(0)
        if ($r -ge 120 -and ($r - $gr) -ge 60 -and ($r - $b) -ge 60) {
            $idx = 2; $cbytes[$i]=0; $cbytes[$i+1]=0; $cbytes[$i+2]=255
        } elseif (($r + $gr + $b) -lt 480) {
            $idx = 1; $cbytes[$i]=0; $cbytes[$i+1]=0; $cbytes[$i+2]=0
        } else {
            $idx = 0; $cbytes[$i]=255; $cbytes[$i+1]=255; $cbytes[$i+2]=255
        }
        $shift = 6 - (($x -band 3) * 2)
        $raw[$po + ($x -shr 2)] = $raw[$po + ($x -shr 2)] -bor ($idx -shl $shift)
    }
}
[System.Runtime.InteropServices.Marshal]::Copy($cbytes, 0, $cbd.Scan0, $cbytes.Length)
$comp.UnlockBits($cbd)
$comp.Save($OutPreviewPng, [System.Drawing.Imaging.ImageFormat]::Png)
$comp.Dispose(); $blank.Dispose(); $src.Dispose()

# Write minimal 2-bit palette PNG (IHDR + PLTE + IDAT + IEND)
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

# Zlib stream: 0x78 0x9C + deflate(raw) + adler32(raw)
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

$ihdr = [byte[]](0,0,3,0xC0, 0,0,2,0xA0, 2, 3, 0, 0, 0) # 960x672, 2-bit, palette
$plte = [byte[]](255,255,255,  0,0,0,  255,0,0,  255,255,255)
$fs = [System.IO.File]::Create($OutFullPng)
$sig = [byte[]](137,80,78,71,13,10,26,10)
$fs.Write($sig, 0, 8)
foreach ($ch in @((New-Chunk 'IHDR' $ihdr), (New-Chunk 'PLTE' $plte), (New-Chunk 'IDAT' $idat), (New-Chunk 'IEND' ([byte[]]@())))) {
    $fs.Write($ch, 0, $ch.Length)
}
$fs.Close()
Write-Output "$OutFullPng $((Get-Item $OutFullPng).Length) bytes (crop ${cropW}x${cropH} -> ${dstW}x${dstH} at $dstX,$dstY)"
