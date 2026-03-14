$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$artifactsDir = Join-Path $repoRoot "artifacts\ux_demo"
$framesPath = Join-Path $artifactsDir "frames.json"

Add-Type -AssemblyName System.Drawing

function New-StyleState {
    return @{
        Foreground = [System.Drawing.Color]::FromArgb(209, 213, 219)
        Background = $null
        Bold = $false
    }
}

function Parse-AnsiRuns {
    param(
        [string]$Line
    )

    $esc = [string][char]27
    $ansiPattern = [regex]::Escape($esc) + "\[[0-9;]*m"
    $matches = [regex]::Matches($Line, $ansiPattern)
    $runs = New-Object System.Collections.Generic.List[object]
    $state = New-StyleState
    $cursor = 0

    foreach ($match in $matches) {
        if ($match.Index -gt $cursor) {
            $text = $Line.Substring($cursor, $match.Index - $cursor)
            if ($text.Length -gt 0) {
                $runs.Add([pscustomobject]@{
                    Text = $text
                    Foreground = $state.Foreground
                    Background = $state.Background
                    Bold = $state.Bold
                })
            }
        }

        $codes = $match.Value.Substring(2, $match.Value.Length - 3).Split(";")
        if ($codes.Count -eq 0 -or ($codes.Count -eq 1 -and $codes[0] -eq "")) {
            $codes = @("0")
        }

        for ($i = 0; $i -lt $codes.Count; $i++) {
            switch ($codes[$i]) {
                "0" {
                    $state = New-StyleState
                }
                "1" {
                    $state.Bold = $true
                }
                "2" {
                    $state.Bold = $false
                }
                "38" {
                    if (($i + 4) -lt $codes.Count -and $codes[$i + 1] -eq "2") {
                        $state.Foreground = [System.Drawing.Color]::FromArgb(
                            [int]$codes[$i + 2],
                            [int]$codes[$i + 3],
                            [int]$codes[$i + 4]
                        )
                        $i += 4
                    }
                }
                "48" {
                    if (($i + 4) -lt $codes.Count -and $codes[$i + 1] -eq "2") {
                        $state.Background = [System.Drawing.Color]::FromArgb(
                            [int]$codes[$i + 2],
                            [int]$codes[$i + 3],
                            [int]$codes[$i + 4]
                        )
                        $i += 4
                    }
                }
            }
        }

        $cursor = $match.Index + $match.Length
    }

    if ($cursor -lt $Line.Length) {
        $text = $Line.Substring($cursor)
        if ($text.Length -gt 0) {
            $runs.Add([pscustomobject]@{
                Text = $text
                Foreground = $state.Foreground
                Background = $state.Background
                Bold = $state.Bold
            })
        }
    }

    return $runs
}

$frames = Get-Content $framesPath -Raw | ConvertFrom-Json
$background = [System.Drawing.Color]::FromArgb(15, 17, 21)
$mutedGrid = [System.Drawing.Color]::FromArgb(24, 28, 34)

foreach ($frame in $frames) {
    $bitmap = New-Object System.Drawing.Bitmap 1800, 1200
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.Clear($background)
    $graphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::ClearTypeGridFit
    $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias

    $brush = New-Object System.Drawing.SolidBrush $mutedGrid
    for ($x = 0; $x -lt 1800; $x += 60) {
        $graphics.DrawLine((New-Object System.Drawing.Pen $brush.Color), $x, 0, $x, 1200)
    }
    for ($y = 0; $y -lt 1200; $y += 60) {
        $graphics.DrawLine((New-Object System.Drawing.Pen $brush.Color), 0, $y, 1800, $y)
    }
    $brush.Dispose()

    $fontRegular = New-Object System.Drawing.Font("Consolas", 24, [System.Drawing.FontStyle]::Regular)
    $fontBold = New-Object System.Drawing.Font("Consolas", 24, [System.Drawing.FontStyle]::Bold)
    $format = [System.Drawing.StringFormat]::GenericTypographic
    $format.FormatFlags = [System.Drawing.StringFormatFlags]::MeasureTrailingSpaces

    $y = 44.0
    foreach ($line in $frame.lines) {
        $runs = Parse-AnsiRuns $line
        $x = 42.0
        foreach ($run in $runs) {
            $font = $fontRegular
            if ($run.Bold) {
                $font = $fontBold
            }

            $size = $graphics.MeasureString($run.Text, $font, 4000, $format)
            if ($run.Background -ne $null) {
                $bgBrush = New-Object System.Drawing.SolidBrush $run.Background
                $graphics.FillRectangle($bgBrush, $x, $y - 2, [math]::Ceiling($size.Width), 34)
                $bgBrush.Dispose()
            }

            $fgBrush = New-Object System.Drawing.SolidBrush $run.Foreground
            $graphics.DrawString($run.Text, $font, $fgBrush, $x, $y, $format)
            $fgBrush.Dispose()
            $x += $size.Width
        }
        $y += 34
    }

    $fontRegular.Dispose()
    $fontBold.Dispose()
    $graphics.Dispose()

    $outputPath = Join-Path $artifactsDir ($frame.name + ".png")
    $bitmap.Save($outputPath, [System.Drawing.Imaging.ImageFormat]::Png)
    $bitmap.Dispose()
}
