#requires -Version 5.1
<#
    Chart generator (dependency-free, System.Drawing). The chat tool "generate_chart"
    runs this script in a separate pwsh process; it reads a JSON spec from stdin and
    writes a JSON result to stdout.

    Supported types:
      bar      : labels + one or more series (grouped bars)
      line     : labels + one or more series (line)
      pie      : labels + a single series (pie)
      scatter  : series.points = [{x;y}, ...] (scatter)
      function : functions = ["sin(x)", ...] (y=f(x) curve; safe expression evaluator)

    Safe expression evaluator: Invoke-Expression is NOT used. Only numbers, x,
    pi, e, + - * / ^ ( ) , and sin cos tan asin acos atan sinh cosh tanh exp ln log
    log10 sqrt abs floor ceil round are accepted; everything else is rejected.
#>
param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

$script:raw = [Console]::In.ReadToEnd()
$spec = $null
try { $spec = $script:raw | ConvertFrom-Json -ErrorAction Stop } catch { }

function Write-Result($obj) {
    $json = $obj | ConvertTo-Json -Compress -Depth 8
    [Console]::Out.Write($json)
}

if (!$spec) { Write-Result @{ error = 'Could not read chart request (invalid JSON).' }; exit 0 }

# ---------------------------------------------------------------- expression evaluator
$script:tokens = @()
$script:pos = 0
$script:funcs = @{
    'sin' = 'Sin'; 'cos' = 'Cos'; 'tan' = 'Tan'; 'asin' = 'Asin'; 'acos' = 'Acos'; 'atan' = 'Atan'
    'sinh' = 'Sinh'; 'cosh' = 'Cosh'; 'tanh' = 'Tanh'; 'exp' = 'Exp'
    'sqrt' = 'Sqrt'; 'abs' = 'Abs'; 'floor' = 'Floor'; 'ceil' = 'Ceiling'; 'round' = 'Round'
    'ln' = 'Log'; 'log' = 'Log'; 'log10' = 'Log10'
}

function Tokenize([string]$expr) {
    $pattern = '\s*(?<tok>[0-9]*\.?[0-9]+|[A-Za-z_][A-Za-z0-9_]*|\*\*|[()+\-*/^,%])'
    $ms = [regex]::Matches($expr, $pattern)
    $toks = @()
    foreach ($m in $ms) { $toks += $m.Groups['tok'].Value }
    # If any character (other than whitespace) is not covered, the expression is invalid.
    $rest = [regex]::Replace($expr, $pattern, '')
    if ($rest.Trim().Length -gt 0) { throw "Invalid character in expression: '$rest'" }
    # NOTE: do not wrap with ','; together with the @(...) in Parse-Expression it caused
    # double-wrapping and turned a multi-token expression into a single element ("sin ( x )").
    return $toks
}

function Peek { if ($script:pos -lt $script:tokens.Count) { return [string]$script:tokens[$script:pos] } return '' }
function Next { $t = Peek; if ($t -ne '') { $script:pos++ } return $t }

function Parse-Primary {
    $t = Peek
    if ($t -eq '') { throw 'Expression ended unexpectedly.' }
    if ($t -match '^[0-9]') { [void](Next); return [pscustomobject]@{ t = 'num'; v = [double]::Parse($t, [Globalization.CultureInfo]::InvariantCulture) } }
    if ($t -eq '(') { [void](Next); $e = Parse-Expr; if ((Next) -ne ')') { throw 'Missing closing parenthesis.' }; return $e }
    if ($t -match '^[A-Za-z_]') {
        [void](Next)
        $name = $t.ToLowerInvariant()
        if ((Peek) -eq '(') {
            if ($name -eq 'x') { throw 'x is not a function.' }
            if (!$script:funcs.ContainsKey($name)) { throw "Unknown function: $name" }
            [void](Next)
            $args = @()
            if ((Peek) -ne ')') {
                $args += Parse-Expr
                while ((Peek) -eq ',') { [void](Next); $args += Parse-Expr }
            }
            if ((Next) -ne ')') { throw "Missing function close: $name" }
            if ($args.Count -ne 1) { throw "Only single-argument functions: $name" }
            return [pscustomobject]@{ t = 'call'; name = $name; arg = $args[0] }
        }
        return [pscustomobject]@{ t = 'var'; name = $name }
    }
    throw "Unexpected symbol: $t"
}

function Parse-Power {
    $base = Parse-Primary
    $t = Peek
    if ($t -eq '^' -or $t -eq '**') {
        [void](Next)
        $exp = Parse-Unary   # right-associative: 2^3^2 = 2^(3^2)
        return [pscustomobject]@{ t = 'bin'; op = '^'; l = $base; r = $exp }
    }
    return $base
}

function Parse-Unary {
    $t = Peek
    if ($t -eq '-') { [void](Next); return [pscustomobject]@{ t = 'neg'; c = Parse-Unary } }
    if ($t -eq '+') { [void](Next); return Parse-Unary }
    return Parse-Power
}

function Test-PrimaryStart([string]$t) {
    return ($t -match '^[0-9]') -or ($t -match '^[A-Za-z_]') -or ($t -eq '(')
}

function Parse-Term {
    $left = Parse-Unary
    while ($true) {
        $t = Peek
        if ($t -eq '*') { [void](Next); $left = [pscustomobject]@{ t = 'bin'; op = '*'; l = $left; r = Parse-Unary } }
        elseif ($t -eq '/') { [void](Next); $left = [pscustomobject]@{ t = 'bin'; op = '/'; l = $left; r = Parse-Unary } }
        elseif (Test-PrimaryStart $t) { $left = [pscustomobject]@{ t = 'bin'; op = '*'; l = $left; r = Parse-Unary } }  # implicit multiplication: 2x, 2(x+1)
        else { break }
    }
    return $left
}

function Parse-Expr {
    $left = Parse-Term
    while ((Peek) -eq '+' -or (Peek) -eq '-') {
        $op = Next
        $left = [pscustomobject]@{ t = 'bin'; op = $op; l = $left; r = Parse-Term }
    }
    return $left
}

function Parse-Expression([string]$expr) {
    if ([string]::IsNullOrWhiteSpace($expr)) { throw 'Empty expression.' }
    $script:tokens = @(Tokenize $expr)
    $script:pos = 0
    $ast = Parse-Expr
    if ($script:pos -ne $script:tokens.Count) { throw "Expression could not be fully parsed (position $($script:pos))." }
    return $ast
}

function Invoke-Node($node, [double]$x) {
    switch ($node.t) {
        'num' { return [double]$node.v }
        'var' {
            switch ($node.name) {
                'x' { return $x }
                'pi' { return [math]::PI }
                'e' { return [math]::E }
                default { throw ("Unknown variable: " + $node.name) }
            }
        }
        'neg' { return (0 - (Invoke-Node $node.c $x)) }
        'bin' {
            $a = Invoke-Node $node.l $x; $b = Invoke-Node $node.r $x
            switch ($node.op) {
                '+' { return ($a + $b) } '-' { return ($a - $b) } '*' { return ($a * $b) }
                '/' { return ($a / $b) } '^' { return [math]::Pow($a, $b) }
                default { throw ("Unknown operator: " + $node.op) }
            }
        }
        'call' {
            $a = Invoke-Node $node.arg $x
            # NOTE: PowerShell does not support a dynamic method name like [math]::$m($a); use switch.
            switch ($node.name) {
                'sin' { return [math]::Sin($a) } 'cos' { return [math]::Cos($a) } 'tan' { return [math]::Tan($a) }
                'asin' { return [math]::Asin($a) } 'acos' { return [math]::Acos($a) } 'atan' { return [math]::Atan($a) }
                'sinh' { return [math]::Sinh($a) } 'cosh' { return [math]::Cosh($a) } 'tanh' { return [math]::Tanh($a) }
                'exp' { return [math]::Exp($a) } 'sqrt' { return [math]::Sqrt($a) } 'abs' { return [math]::Abs($a) }
                'floor' { return [math]::Floor($a) } 'ceil' { return [math]::Ceiling($a) } 'round' { return [math]::Round($a) }
                'ln' { return [math]::Log($a) } 'log' { return [math]::Log($a) } 'log10' { return [math]::Log10($a) }
                default { throw ("Unknown function: " + $node.name) }
            }
        }
    }
}

# ---------------------------------------------------------------- drawing helpers
$script:Palette = @(
    [Drawing.Color]::FromArgb(0, 112, 209), [Drawing.Color]::FromArgb(217, 130, 43),
    [Drawing.Color]::FromArgb(40, 167, 69), [Drawing.Color]::FromArgb(214, 51, 108),
    [Drawing.Color]::FromArgb(111, 66, 193), [Drawing.Color]::FromArgb(23, 162, 184)
)
$colInk = [Drawing.Color]::FromArgb(10, 26, 51)
$colMuted = [Drawing.Color]::FromArgb(110, 120, 140)
$colGrid = [Drawing.Color]::FromArgb(224, 230, 239)
$colBg = [Drawing.Color]::FromArgb(255, 255, 255)

function Format-Num([double]$v) {
    if ([math]::Abs($v) -ge 100000 -or ([math]::Abs($v) -lt 0.01 -and $v -ne 0)) { return $v.ToString('0.##E+0', [Globalization.CultureInfo]::InvariantCulture) }
    if ([math]::Abs($v - [math]::Round($v)) -lt 1e-9) { return ([math]::Round($v)).ToString([Globalization.CultureInfo]::InvariantCulture) }
    return $v.ToString('0.###', [Globalization.CultureInfo]::InvariantCulture)
}

function New-Font([single]$size, [bool]$bold = $false) {
    $style = if ($bold) { [Drawing.FontStyle]::Bold } else { [Drawing.FontStyle]::Regular }
    return New-Object Drawing.Font('Segoe UI', $size, $style)
}

function Draw-Text($g, [string]$text, [single]$x, [single]$y, [Drawing.Font]$font, [Drawing.Color]$color, [string]$align = 'near', [string]$valign = 'near') {
    if ([string]::IsNullOrEmpty($text)) { return }
    $sz = $g.MeasureString($text, $font)
    $px = switch ($align) { 'center' { $x - $sz.Width / 2 } 'far' { $x - $sz.Width } default { $x } }
    $py = switch ($valign) { 'center' { $y - $sz.Height / 2 } 'far' { $y - $sz.Height } default { $y } }
    $brush = New-Object Drawing.SolidBrush($color)
    try { $g.DrawString($text, $font, $brush, [single]$px, [single]$py) } finally { $brush.Dispose() }
}

function New-Canvas([int]$w, [int]$h, [string]$title) {
    $bmp = New-Object Drawing.Bitmap($w, $h)
    $g = [Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = 'AntiAlias'
    $g.TextRenderingHint = 'ClearTypeGridFit'
    $g.Clear($colBg)
    if ($title) { Draw-Text $g $title ($w / 2) 22 (New-Font 15 $true) $colInk 'center' }
    return @{ bmp = $bmp; g = $g }
}

function Save-Canvas($canvas, [string]$out) {
    $dir = Split-Path -Parent $out
    if ($dir -and !(Test-Path -LiteralPath $dir -PathType Container)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $canvas.bmp.Save($out, [Drawing.Imaging.ImageFormat]::Png)
    $canvas.g.Dispose()
    $canvas.bmp.Dispose()
}

function Draw-Axes($g, [int]$l, [int]$t, [int]$pw, [int]$ph, [double]$xmin, [double]$xmax, [double]$ymin, [double]$ymax, [string]$xlabel, [string]$ylabel) {
    $penAxis = New-Object Drawing.Pen($colInk, 1.4)
    $penGrid = New-Object Drawing.Pen($colGrid, 1)
    $fontTick = New-Font 9
    try {
        for ($i = 0; $i -le 4; $i++) {
            $yv = $ymin + ($ymax - $ymin) * $i / 4
            $py = $t + $ph - ($ph * ($yv - $ymin) / ($ymax - $ymin))
            $g.DrawLine($penGrid, $l, [single]$py, $l + $pw, [single]$py)
            Draw-Text $g (Format-Num $yv) ($l - 8) $py $fontTick $colMuted 'far' 'center'
        }
        for ($i = 0; $i -le 4; $i++) {
            $xv = $xmin + ($xmax - $xmin) * $i / 4
            $px = $l + $pw * ($xv - $xmin) / ($xmax - $xmin)
            $g.DrawLine($penGrid, [single]$px, $t, [single]$px, $t + $ph)
            Draw-Text $g (Format-Num $xv) $px ($t + $ph + 6) $fontTick $colMuted 'center'
        }
        # Zero lines
        if ($ymin -lt 0 -and $ymax -gt 0) {
            $py0 = $t + $ph - ($ph * (0 - $ymin) / ($ymax - $ymin))
            $g.DrawLine($penAxis, $l, [single]$py0, $l + $pw, [single]$py0)
        }
        if ($xmin -lt 0 -and $xmax -gt 0) {
            $px0 = $l + $pw * (0 - $xmin) / ($xmax - $xmin)
            $g.DrawLine($penAxis, [single]$px0, $t, [single]$px0, $t + $ph)
        }
        $g.DrawRectangle($penAxis, $l, $t, $pw, $ph)
        if ($xlabel) { Draw-Text $g $xlabel ($l + $pw / 2) ($t + $ph + 26) (New-Font 10) $colInk 'center' }
        if ($ylabel) {
            $save = $g.Save()
            $g.TranslateTransform(($l - 46), ($t + $ph / 2))
            $g.RotateTransform(-90)
            Draw-Text $g $ylabel 0 0 (New-Font 10) $colInk 'center'
            $g.Restore($save)
        }
    } finally {
        $penAxis.Dispose(); $penGrid.Dispose(); $fontTick.Dispose()
    }
}

function Draw-Legend($g, [string[]]$names, [int]$right, [int]$top) {
    if (!$names -or $names.Count -eq 0) { return }
    $font = New-Font 9.5
    $h = 20
    try {
        $y = $top
        for ($i = 0; $i -lt $names.Count; $i++) {
            $brush = New-Object Drawing.SolidBrush($script:Palette[$i % $script:Palette.Count])
            try { $g.FillRectangle($brush, $right - 150, $y, 12, 12) } finally { $brush.Dispose() }
            Draw-Text $g $names[$i] ($right - 132) ($y - 2) $font $colInk
            $y += $h
        }
    } finally { $font.Dispose() }
}

# ---------------------------------------------------------------- types
function Build-Function($spec, $canvas, [int]$l, [int]$t, [int]$pw, [int]$ph) {
    $g = $canvas.g
    $exprs = @()
    if ($spec.functions) { $exprs = @($spec.functions) } elseif ($spec.function) { $exprs = @([string]$spec.function) }
    if ($exprs.Count -eq 0) { throw 'No function expression provided.' }
    $xmin = if ($null -ne $spec.xmin) { [double]$spec.xmin } else { -10.0 }
    $xmax = if ($null -ne $spec.xmax) { [double]$spec.xmax } else { 10.0 }
    if ($xmax -le $xmin) { throw 'xmax must be greater than xmin.' }
    $samples = if ($spec.samples) { [int]$spec.samples } else { 600 }
    if ($samples -lt 50) { $samples = 50 }
    $names = if ($spec.labels) { @($spec.labels) } else { $exprs }

    $compiled = @()
    foreach ($e in $exprs) { $compiled += (Parse-Expression ([string]$e)) }

    # Sample
    $seriesPts = @()
    $ymin = [double]::PositiveInfinity; $ymax = [double]::NegativeInfinity
    foreach ($ast in $compiled) {
        $pts = New-Object System.Collections.Generic.List[object]
        for ($i = 0; $i -le $samples; $i++) {
            $x = $xmin + ($xmax - $xmin) * $i / $samples
            $y = $null
            try { $y = Invoke-Node $ast $x } catch { $y = $null; if (-not $script:sampleErr) { $script:sampleErr = $_.Exception.Message } }
            if ($null -ne $y -and ![double]::IsNaN($y) -and ![double]::IsInfinity($y)) {
                $pts.Add(@{ x = $x; y = [double]$y })
                if ($y -lt $ymin) { $ymin = [double]$y }
                if ($y -gt $ymax) { $ymax = [double]$y }
            } else {
                $pts.Add($null)  # gap
            }
        }
        $seriesPts += , $pts
    }
    if ($ymin -eq [double]::PositiveInfinity) { throw ("Function produced no valid value in the given range. " + [string]$script:sampleErr) }
    if ($ymax -eq $ymin) { $ymax = $ymin + 1 }
    $pad = ($ymax - $ymin) * 0.06
    $ymin -= $pad; $ymax += $pad

    Draw-Axes $g $l $t $pw $ph $xmin $xmax $ymin $ymax ([string]$spec.xlabel) ([string]$spec.ylabel)

    $MapX = { param($xv) $l + $pw * ($xv - $xmin) / ($xmax - $xmin) }
    $MapY = { param($yv) $t + $ph - $ph * ($yv - $ymin) / ($ymax - $ymin) }

    for ($s = 0; $s -lt $compiled.Count; $s++) {
        $pen = New-Object Drawing.Pen($script:Palette[$s % $script:Palette.Count], 2.2)
        $pen.LineJoin = 'Round'
        try {
            $prev = $null
            foreach ($p in $seriesPts[$s]) {
                if ($null -eq $p) { $prev = $null; continue }
                $px = & $MapX $p.x; $py = & $MapY $p.y
                # Clip vertical lines at extreme values
                if ($py -lt $t - 4) { $py = $t - 4 } elseif ($py -gt $t + $ph + 4) { $py = $t + $ph + 4 }
                if ($null -ne $prev) { $g.DrawLine($pen, [single]$prev[0], [single]$prev[1], [single]$px, [single]$py) }
                $prev = @($px, $py)
            }
        } finally { $pen.Dispose() }
    }
    Draw-Legend $g $names ($l + $pw - 10) ($t + 8)
}

function Get-PointXY($p) {
    # A point can be a {x;y} object or an [x, y] array (ConvertFrom-Json produces both).
    if ($p -is [array]) { return @([double]$p[0], [double]$p[1]) }
    return @([double]$p.x, [double]$p.y)
}

function Build-Data($spec, $canvas, [int]$l, [int]$t, [int]$pw, [int]$ph) {
    $g = $canvas.g
    $type = ([string]$spec.type).ToLowerInvariant()
    $labels = @($spec.labels | Where-Object { $null -ne $_ })
    # Series list: 'series' (multiple) or top-level 'values' (single series) for convenience.
    # Small models often give values directly for a single series; accept both.
    $series = @()
    if ($null -ne $spec.series) { $series = @($spec.series | Where-Object { $null -ne $_ }) }
    if ($series.Count -eq 0 -and $type -eq 'scatter' -and $null -ne $spec.points) {
        $series = @([pscustomobject]@{ name = 'Points'; points = @($spec.points) })
    }
    if ($series.Count -eq 0 -and $null -ne $spec.values) {
        $series = @([pscustomobject]@{ name = 'Value'; values = @($spec.values) })
    }
    if ($series.Count -eq 0) { throw 'No series data (series or values required).' }

    if ($type -eq 'pie') {
        $values = @($series[0].values | Where-Object { $null -ne $_ } | ForEach-Object { [double]$_ })
        $total = ($values | Measure-Object -Sum).Sum
        if ($total -le 0) { throw 'Pie chart requires positive values.' }
        $cx = $l + $pw / 2; $cy = $t + $ph / 2
        $radius = [math]::Min($pw, $ph) / 2 - 10
        $start = -90.0
        for ($i = 0; $i -lt $values.Count; $i++) {
            $sweep = 360.0 * $values[$i] / $total
            $brush = New-Object Drawing.SolidBrush($script:Palette[$i % $script:Palette.Count])
            try { $g.FillPie($brush, [single]($cx - $radius), [single]($cy - $radius), [single](2 * $radius), [single](2 * $radius), [single]$start, [single]$sweep) } finally { $brush.Dispose() }
            $mid = ($start + $sweep / 2) * [math]::PI / 180
            $lx = $cx + [math]::Cos($mid) * ($radius * 0.62)
            $ly = $cy + [math]::Sin($mid) * ($radius * 0.62)
            $pct = [math]::Round(100.0 * $values[$i] / $total, 1)
            if ($sweep -gt 12) { Draw-Text $g ("$pct%") $lx $ly (New-Font 10 $true) ([Drawing.Color]::White) 'center' 'center' }
            $start += $sweep
        }
        Draw-Legend $g (@($labels) ) ($l + $pw - 10) ($t + 8)
        return
    }

    if ($type -eq 'scatter') {
        # min/max over all points
        $xs = New-Object System.Collections.Generic.List[double]
        $ys = New-Object System.Collections.Generic.List[double]
        foreach ($s in $series) { foreach ($p in @($s.points)) { $xy = Get-PointXY $p; $xs.Add($xy[0]); $ys.Add($xy[1]) } }
        if ($xs.Count -eq 0) { throw 'No points for scatter.' }
        $xmin = ($xs | Measure-Object -Minimum).Minimum; $xmax = ($xs | Measure-Object -Maximum).Maximum
        $ymin = ($ys | Measure-Object -Minimum).Minimum; $ymax = ($ys | Measure-Object -Maximum).Maximum
        if ($xmax -eq $xmin) { $xmax = $xmin + 1 }; if ($ymax -eq $ymin) { $ymax = $ymin + 1 }
        $xpad = ($xmax - $xmin) * 0.05; $ypad = ($ymax - $ymin) * 0.08
        $xmin -= $xpad; $xmax += $xpad; $ymin -= $ypad; $ymax += $ypad
        Draw-Axes $g $l $t $pw $ph $xmin $xmax $ymin $ymax ([string]$spec.xlabel) ([string]$spec.ylabel)
        $names = @()
        for ($s = 0; $s -lt $series.Count; $s++) {
            $names += [string]$series[$s].name
            $brush = New-Object Drawing.SolidBrush($script:Palette[$s % $script:Palette.Count])
            try {
                foreach ($p in @($series[$s].points)) {
                    $xy = Get-PointXY $p
                    $px = $l + $pw * ($xy[0] - $xmin) / ($xmax - $xmin)
                    $py = $t + $ph - $ph * ($xy[1] - $ymin) / ($ymax - $ymin)
                    $g.FillEllipse($brush, [single]($px - 4), [single]($py - 4), 8, 8)
                }
            } finally { $brush.Dispose() }
        }
        Draw-Legend $g $names ($l + $pw - 10) ($t + 8)
        return
    }

    # bar / line: labels + series
    $allVals = @()
    foreach ($s in $series) { $allVals += @($s.values | ForEach-Object { [double]$_ }) }
    if ($allVals.Count -eq 0) { throw 'Series values are empty.' }
    $ymin = [math]::Min(0, ($allVals | Measure-Object -Minimum).Minimum)
    $ymax = [math]::Max(0, ($allVals | Measure-Object -Maximum).Maximum)
    if ($ymax -eq $ymin) { $ymax = $ymin + 1 }
    $ymax += ($ymax - $ymin) * 0.08
    $n = if ($labels.Count -gt 0) { $labels.Count } else { @($series[0].values).Count }
    Draw-Axes $g $l $t $pw $ph 0 $n $ymin $ymax ([string]$spec.xlabel) ([string]$spec.ylabel)

    $MapY = { param($yv) $t + $ph - $ph * ($yv - $ymin) / ($ymax - $ymin) }
    if ($type -eq 'bar') {
        $groups = $series.Count
        $slot = $pw / [math]::Max(1, $n)
        $barW = ($slot * 0.7) / [math]::Max(1, $groups)
        $y0 = & $MapY 0
        for ($c = 0; $c -lt $n; $c++) {
            for ($s = 0; $s -lt $groups; $s++) {
                $v = [double](@($series[$s].values)[$c])
                $px = $l + $c * $slot + $slot * 0.15 + $s * $barW
                $py = & $MapY $v
                $top = [math]::Min($py, $y0); $hgt = [math]::Abs($y0 - $py)
                $brush = New-Object Drawing.SolidBrush($script:Palette[$s % $script:Palette.Count])
                try { $g.FillRectangle($brush, [single]$px, [single]$top, [single]$barW, [single]$hgt) } finally { $brush.Dispose() }
            }
            if ($c -lt $labels.Count) { Draw-Text $g ([string]$labels[$c]) ($l + $c * $slot + $slot / 2) ($t + $ph + 6) (New-Font 9) $colMuted 'center' }
        }
    } else {
        for ($s = 0; $s -lt $series.Count; $s++) {
            $pen = New-Object Drawing.Pen($script:Palette[$s % $script:Palette.Count], 2.2)
            $pen.LineJoin = 'Round'
            try {
                $prev = $null
                for ($c = 0; $c -lt $n; $c++) {
                    $v = [double](@($series[$s].values)[$c])
                    $px = $l + ($pw / [math]::Max(1, $n - 1)) * $c + ($pw / [math]::Max(1, $n)) * 0.5
                    $py = & $MapY $v
                    if ($null -ne $prev) { $g.DrawLine($pen, [single]$prev[0], [single]$prev[1], [single]$px, [single]$py) }
                    $prev = @($px, $py)
                }
            } finally { $pen.Dispose() }
        }
        for ($c = 0; $c -lt $n; $c++) {
            if ($c -lt $labels.Count) {
                $px = $l + ($pw / [math]::Max(1, $n - 1)) * $c + ($pw / [math]::Max(1, $n)) * 0.5
                Draw-Text $g ([string]$labels[$c]) $px ($t + $ph + 6) (New-Font 9) $colMuted 'center'
            }
        }
    }
    Draw-Legend $g (@($series | ForEach-Object { [string]$_.name })) ($l + $pw - 10) ($t + 8)
}

# ---------------------------------------------------------------- main flow
try {
    $type = ([string]$spec.type).ToLowerInvariant()
    $w = if ($spec.width) { [int]$spec.width } else { 1100 }
    $h = if ($spec.height) { [int]$spec.height } else { 680 }
    if ($w -lt 320) { $w = 320 }; if ($w -gt 2400) { $w = 2400 }
    if ($h -lt 240) { $h = 240 }; if ($h -gt 1600) { $h = 1600 }
    $out = [string]$spec.out
    if (!$out) { throw 'Output path (out) is required.' }

    $canvas = New-Canvas $w $h ([string]$spec.title)
    $l = 70; $t = 60; $r = 30; $b = 70
    $pw = $w - $l - $r; $ph = $h - $t - $b

    switch ($type) {
        'function' { Build-Function $spec $canvas $l $t $pw $ph }
        'bar' { Build-Data $spec $canvas $l $t $pw $ph }
        'line' { Build-Data $spec $canvas $l $t $pw $ph }
        'pie' { Build-Data $spec $canvas $l $t $pw $ph }
        'scatter' { Build-Data $spec $canvas $l $t $pw $ph }
        default { throw "Unsupported chart type: $type" }
    }
    Save-Canvas $canvas $out
    Write-Result @{ path = $out; width = $w; height = $h; type = $type }
} catch {
    Write-Result @{ error = $_.Exception.Message }
}
