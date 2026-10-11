# Gera as fixtures de áudio (16 kHz, mono, 16-bit) com System.Speech.
# Uso: powershell -File scripts\gerar_fixture.ps1
Add-Type -AssemblyName System.Speech

$raiz = Split-Path -Parent $PSScriptRoot
$destino = Join-Path $raiz 'tests\fixtures'
New-Item -ItemType Directory -Force $destino | Out-Null

function Gerar($voz, $frase, $nome) {
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    try {
        $synth.SelectVoice($voz)
    } catch {
        Write-Warning "Voz '$voz' indisponível; pulando $nome"
        return
    }
    $fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
        16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono)
    $wav = Join-Path $destino "$nome.wav"
    $synth.SetOutputToWaveFile($wav, $fmt)
    $synth.Speak($frase)
    $synth.Dispose()
    [System.IO.File]::WriteAllText((Join-Path $destino "$nome.txt"), $frase, (New-Object System.Text.UTF8Encoding($false)))
    Write-Output "Gerado: $wav"
}

Gerar 'Microsoft Maria Desktop' 'Olá, este é um teste do Hear AI.' 'frase_pt'
Gerar 'Microsoft Zira Desktop' 'Hello, this is a test of Hear AI.' 'frase_en'
