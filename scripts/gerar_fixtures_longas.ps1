# Gera as fixtures longas (16 kHz, mono, 16-bit) a partir dos textos versionados em
# tests\fixtures\longas\fala_*.txt, com a voz "Microsoft Maria Desktop".
# Cada linha em branco (fim de parágrafo) vira uma pausa de 1,5 s de silêncio.
# Os .wav NÃO são versionados (são grandes); rode este script para recriá-los.
# Uso: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\gerar_fixtures_longas.ps1
Add-Type -AssemblyName System.Speech

$raiz = Split-Path -Parent $PSScriptRoot
$pasta = Join-Path $raiz 'tests\fixtures\longas'
$pausa = [TimeSpan]::FromMilliseconds(1500)
$utf8 = New-Object System.Text.UTF8Encoding($false)

$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
    16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono)

foreach ($txt in Get-ChildItem -Path $pasta -Filter 'fala_*.txt' | Sort-Object Name) {
    $texto = [System.IO.File]::ReadAllText($txt.FullName, $utf8)
    $paragrafos = $texto -split "(\r?\n){2,}" | ForEach-Object { $_.Trim() } | Where-Object { $_ }

    $pb = New-Object System.Speech.Synthesis.PromptBuilder([System.Globalization.CultureInfo]::GetCultureInfo('pt-BR'))
    foreach ($p in $paragrafos) {
        $pb.AppendText($p)
        $pb.AppendBreak($pausa)
    }

    $wav = [System.IO.Path]::ChangeExtension($txt.FullName, '.wav')
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    try {
        $synth.SelectVoice('Microsoft Maria Desktop')
        $synth.SetOutputToWaveFile($wav, $fmt)
        $synth.Speak($pb)
    } finally {
        $synth.Dispose()
    }
    $bytes = (Get-Item $wav).Length
    $segundos = [Math]::Round(($bytes - 44) / 32000.0, 1)
    Write-Output ("{0}: {1} s, {2:N1} MB" -f (Split-Path -Leaf $wav), $segundos, ($bytes / 1MB))
}
