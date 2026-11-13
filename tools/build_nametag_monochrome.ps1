<#
Converts every PNG in assets/nametag into a 960x336 band for the Nametag app.

  name-v<N>-bw-source.png -> device/data/graphics/nametag_band_v<N>.png
      Lettering. Thresholded with no dithering so the letters stay crisp.
  any other *.png         -> device/data/nametag/<same name>.png
      Artwork. Atkinson-dithered so shading survives the downscale.

The Nametag app shows nametag_band_v1 between each image in /data/nametag,
in file-name order. To add an image, drop a PNG into assets/nametag, run this
script, then push (tools\dev.py push). Sources that are not 20:7 are fitted
inside the band and padded with white.

Device format: 960x336, 1-bit grayscale, filter 0, not interlaced; a white
sample is 1 and a black sample is 0. No gray or red pixels.
#>
param(
    [string]$SourceDir = (Join-Path $PSScriptRoot '../assets/nametag'),
    [string]$OutputDir = (Join-Path $PSScriptRoot '../device/data/graphics'),
    [string]$ImageDir = (Join-Path $PSScriptRoot '../device/data/nametag')
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing, System.IO.Compression

# BEGIN NametagBand (C#, kept to C# 5 so Windows PowerShell 5.1 can compile it)
$nametagBandSource = @'
using System;

namespace Supercon10
{
    public static class NametagBand
    {
        public const int Width = 960;
        public const int Height = 336;
        public const int RowBytes = Width / 8 + 1;  // filter byte + 120 data bytes
        const int Scale = 256;                      // gray levels are 0 .. 255 * Scale
        const int White = 255 * Scale;
        const int Midpoint = 128 * Scale;

        // 32bpp BGRA rows (GDI+ Format32bppArgb, straight alpha) -> 8-bit luma on white.
        public static byte[] LumaOnWhite(byte[] bgra, int stride, int width, int height)
        {
            byte[] luma = new byte[width * height];
            for (int y = 0; y < height; y++)
            {
                for (int x = 0; x < width; x++)
                {
                    int i = y * stride + x * 4;
                    int a = bgra[i + 3];
                    int pad = 255 * (255 - a) + 127;
                    int b = (bgra[i] * a + pad) / 255;
                    int g = (bgra[i + 1] * a + pad) / 255;
                    int r = (bgra[i + 2] * a + pad) / 255;
                    luma[y * width + x] = (byte)((299 * r + 587 * g + 114 * b + 500) / 1000);
                }
            }
            return luma;
        }

        // Size of the source once fitted inside the band, keeping its aspect ratio.
        public static int[] FitSize(int sourceWidth, int sourceHeight)
        {
            long sw = sourceWidth, sh = sourceHeight;
            if (sw * Height >= sh * Width)
                return new int[] { Width, (int)Math.Max(1, (sh * Width * 2 + sw) / (2 * sw)) };
            return new int[] { (int)Math.Max(1, (sw * Height * 2 + sh) / (2 * sh)), Height };
        }

        // Area-average (box filter) the luma into the band, centered on white.
        // Each output pixel is the exact coverage-weighted mean of the source.
        public static int[] Fit(byte[] luma, int sourceWidth, int sourceHeight)
        {
            int[] size = FitSize(sourceWidth, sourceHeight);
            int dw = size[0], dh = size[1];
            int left = (Width - dw) / 2, top = (Height - dh) / 2;

            long[] across = new long[sourceHeight * dw];
            for (int y = 0; y < sourceHeight; y++)
            {
                for (int j = 0; j < dw; j++)
                {
                    long a = (long)j * sourceWidth, b = (long)(j + 1) * sourceWidth;
                    long sum = 0;
                    for (long i = a / dw; i <= (b - 1) / dw; i++)
                    {
                        long w = Math.Min(b, (i + 1) * dw) - Math.Max(a, i * dw);
                        sum += w * luma[y * sourceWidth + (int)i];
                    }
                    across[y * dw + j] = sum;
                }
            }

            int[] gray = new int[Width * Height];
            for (int i = 0; i < gray.Length; i++) gray[i] = White;
            long area = (long)sourceWidth * sourceHeight;
            for (int k = 0; k < dh; k++)
            {
                long a = (long)k * sourceHeight, b = (long)(k + 1) * sourceHeight;
                for (int j = 0; j < dw; j++)
                {
                    long sum = 0;
                    for (long y = a / dh; y <= (b - 1) / dh; y++)
                    {
                        long w = Math.Min(b, (y + 1) * dh) - Math.Max(a, y * dh);
                        sum += w * across[(int)y * dw + j];
                    }
                    gray[(top + k) * Width + left + j] = (int)((sum * Scale + area / 2) / area);
                }
            }
            return gray;
        }

        // Band gray levels -> PNG scanlines. Threshold at mid-gray, or Atkinson
        // dither: 1/8 of the error to each of six neighbours (6/8 kept in total,
        // which holds solid blacks and whites clean on the e-ink panel).
        public static byte[] ToScanlines(int[] gray, bool dither)
        {
            int[] work = (int[])gray.Clone();
            byte[] raw = new byte[Height * RowBytes];
            for (int y = 0; y < Height; y++)
            {
                for (int x = 0; x < Width; x++)
                {
                    int value = work[y * Width + x];
                    bool white = value >= Midpoint;
                    if (white)
                        raw[y * RowBytes + 1 + (x >> 3)] |= (byte)(128 >> (x & 7));
                    if (!dither) continue;
                    int share = (value - (white ? White : 0)) >> 3;
                    Spread(work, x + 1, y, share);
                    Spread(work, x + 2, y, share);
                    Spread(work, x - 1, y + 1, share);
                    Spread(work, x, y + 1, share);
                    Spread(work, x + 1, y + 1, share);
                    Spread(work, x, y + 2, share);
                }
            }
            return raw;
        }

        static void Spread(int[] work, int x, int y, int share)
        {
            if (x >= 0 && x < Width && y < Height) work[y * Width + x] += share;
        }

        public static int CountBlack(byte[] raw)
        {
            int black = 0;
            for (int y = 0; y < Height; y++)
                for (int x = 1; x < RowBytes; x++)
                {
                    int v = raw[y * RowBytes + x];
                    for (int bit = 0; bit < 8; bit++) if ((v & (1 << bit)) == 0) black++;
                }
            return black;
        }

        public static uint Crc32(byte[] data)
        {
            uint crc = 0xFFFFFFFF;
            foreach (byte value in data)
            {
                crc ^= value;
                for (int bit = 0; bit < 8; bit++)
                    crc = (crc & 1) != 0 ? (crc >> 1) ^ 0xEDB88320 : crc >> 1;
            }
            return crc ^ 0xFFFFFFFF;
        }

        public static uint Adler32(byte[] data)
        {
            uint s1 = 1, s2 = 0;
            foreach (byte value in data)
            {
                s1 = (s1 + value) % 65521;
                s2 = (s2 + s1) % 65521;
            }
            return (s2 << 16) | s1;
        }
    }
}
'@
# END NametagBand
if (-not ('Supercon10.NametagBand' -as [type])) {
    Add-Type -TypeDefinition $nametagBandSource
}
$band = [Supercon10.NametagBand]

function Write-U32($Stream, [int64]$Value) {
    foreach ($shift in @(24, 16, 8, 0)) { $Stream.WriteByte(($Value -shr $shift) -band 255) }
}

function Write-Chunk($Stream, [string]$Type, [byte[]]$Data) {
    $typeBytes = [Text.Encoding]::ASCII.GetBytes($Type)
    $payload = [byte[]]::new(4 + $Data.Length)
    [Array]::Copy($typeBytes, 0, $payload, 0, 4)
    [Array]::Copy($Data, 0, $payload, 4, $Data.Length)
    Write-U32 $Stream $Data.Length
    $Stream.Write($payload, 0, $payload.Length)
    Write-U32 $Stream ($band::Crc32($payload))
}

function Read-Luma([string]$Path) {
    $source = [Drawing.Bitmap]::new($Path)
    try {
        $rectangle = [Drawing.Rectangle]::new(0, 0, $source.Width, $source.Height)
        $locked = $source.LockBits($rectangle, [Drawing.Imaging.ImageLockMode]::ReadOnly, [Drawing.Imaging.PixelFormat]::Format32bppArgb)
        try {
            $pixels = [byte[]]::new($locked.Stride * $source.Height)
            [Runtime.InteropServices.Marshal]::Copy($locked.Scan0, $pixels, 0, $pixels.Length)
            $luma = $band::LumaOnWhite($pixels, $locked.Stride, $source.Width, $source.Height)
        } finally { $source.UnlockBits($locked) }
        return @{ Luma = $luma; Width = $source.Width; Height = $source.Height }
    } finally { $source.Dispose() }
}

function Write-BandPng([string]$Path, [byte[]]$Raw) {
    $compressed = [IO.MemoryStream]::new()
    $compressed.WriteByte(0x78); $compressed.WriteByte(0x9C)
    $deflate = [IO.Compression.DeflateStream]::new($compressed, [IO.Compression.CompressionMode]::Compress, $true)
    $deflate.Write($Raw, 0, $Raw.Length)
    $deflate.Dispose()
    Write-U32 $compressed ($band::Adler32($Raw))
    $idat = $compressed.ToArray()
    $compressed.Dispose()

    $output = [IO.File]::Create($Path)
    try {
        $signature = [byte[]](137, 80, 78, 71, 13, 10, 26, 10)
        $output.Write($signature, 0, 8)
        Write-Chunk $output 'IHDR' ([byte[]](0,0,3,0xC0, 0,0,1,0x50, 1,0,0,0,0))
        Write-Chunk $output 'IDAT' $idat
        Write-Chunk $output 'IEND' ([byte[]]@())
    } finally { $output.Dispose() }
}

$sources = @(Get-ChildItem -LiteralPath $SourceDir -Filter '*.png' -File | Sort-Object Name)
if ($sources.Count -eq 0) { throw "No PNG files in $SourceDir" }
foreach ($dir in @($OutputDir, $ImageDir)) { [void](New-Item -ItemType Directory -Force -Path $dir) }
$OutputDir = (Resolve-Path -LiteralPath $OutputDir).ProviderPath
$ImageDir = (Resolve-Path -LiteralPath $ImageDir).ProviderPath

$written = @{}
foreach ($file in $sources) {
    if ($file.Name -match '^name-v(\d+)-bw-source\.png$') {
        $outputPath = Join-Path $OutputDir "nametag_band_v$($Matches[1]).png"
        $dither = $false
    } else {
        $outputPath = Join-Path $ImageDir $file.Name
        $dither = $true
    }

    $image = Read-Luma $file.FullName
    $gray = $band::Fit($image.Luma, $image.Width, $image.Height)
    $raw = $band::ToScanlines($gray, $dither)
    Write-BandPng $outputPath $raw
    $written[$outputPath] = $true

    $method = if ($dither) { 'dithered' } else { 'thresholded' }
    $fit = $band::FitSize($image.Width, $image.Height)
    $note = if ($fit[0] -ne 960 -or $fit[1] -ne 336) { ", fitted to $($fit[0])x$($fit[1]) on white" } else { '' }
    Write-Output "$outputPath : 960x336, 1-bit, $method, $($band::CountBlack($raw)) black pixels$note"
}

# Every PNG in $ImageDir goes into the Nametag rotation, so point out leftovers.
$stale = @(Get-ChildItem -LiteralPath $ImageDir -Filter '*.png' -File | Where-Object { -not $written.ContainsKey($_.FullName) })
foreach ($file in $stale) {
    Write-Warning "$($file.FullName) has no source in $SourceDir but is still in the rotation; delete it here and on the badge if it is no longer wanted."
}
