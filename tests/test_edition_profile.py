"""Profiller penceresindeki dil profili (Türkçe/English) seçimi.

Bu sürümün kendi dili ComboBox'ta varsayılan seçilidir; kullanıcı DİĞER dil
sürümünün kurulum paketini (aynı klasöre kurulur) indirip kurabilir ve indirdiği
paketi silebilir.

Önemli tuzak: Show-ProfileManager içindeki tıklama işleyicileri
`GetNewClosure()` ile oluşturulur. Bir kapanış İÇİNDE `$script:` değişkenler
GÖRÜNMEZ (kapanış kendi modül kapsamına bakar, `$script:` boş döner); bu yüzden
sürüm dili ve diğer sürüm deposu yerel değişkene alınmalıdır (`$mainForm = $form`
deseni, AGENTS.md tuzak 21). Aksi halde "zaten kurulu" denetimi ve indirilecek
depo boş kalır. Bu dosya o davranışı hem statik hem de gerçek pwsh ile doğrular.

Test dosyası TR ve ENG depolarında BİREBİR aynıdır; sürüm dilini/reposunu
`agent/profile-catalog.ps1`'in kendisinden okur.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Bu iki depo birbirinin dil sürümüdür; her biri kendi dilini ve karşı depoyu taşır.
EXPECTED = {
    "workaybarsh/MamidLabsTR": ("tr", "workaybarsh/MamidLabsENG"),
    "workaybarsh/MamidLabsENG": ("en", "workaybarsh/MamidLabsTR"),
}


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def edition_values():
    catalog = read_text("agent/profile-catalog.ps1")
    own_repo = re.search(r"\$script:MamidLabsRepo = '([^']+)'", catalog).group(1)
    lang = re.search(r"\$script:MamidLabsEditionLang = '(\w+)'", catalog).group(1)
    other_repo = re.search(r"\$script:OtherEditionRepo = '([^']+)'", catalog).group(1)
    return own_repo, lang, other_repo


_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
$catalog = '__CATALOG__'

function Get-Functions([string]$path) {
    $tokens = $null; $errs = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errs)
    if ($errs.Count) { throw ($errs | Out-String) }
    return (($ast.EndBlock.Statements | Where-Object {
        $_ -is [Management.Automation.Language.FunctionDefinitionAst]
    } | ForEach-Object { $_.Extent.Text }) -join "`n")
}

# `$control.Add_<event>({ ... }.GetNewClosure())` içindeki asıl scriptblock metnini çıkarır.
function Get-ClosureBody([string]$path, [string]$control, [string]$event) {
    $tokens = $null; $errs = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errs)
    if ($errs.Count) { throw ($errs | Out-String) }
    $node = $ast.FindAll({ param($n)
        $n -is [Management.Automation.Language.InvokeMemberExpressionAst] -and
        $n.Expression.Extent.Text -eq $control -and $n.Member.Value -eq ('Add_' + $event)
    }, $true) | Select-Object -First 1
    if (!$node) { throw "Missing handler: $control.$event" }
    $sb = $node.Arguments[0].FindAll({
        param($n) $n -is [Management.Automation.Language.ScriptBlockExpressionAst]
    }, $true) | Select-Object -First 1
    if (!$sb) { throw "No scriptblock in $control.$event" }
    # Dış süslü parantezleri at: [scriptblock]::Create gövdeyi BEKLER; aksi halde
    # iç içe bir scriptblock değişmezi oluşur ve çalıştırma sadece metni yazdırır.
    $text = $sb.Extent.Text.Trim()
    if ($text.StartsWith('{') -and $text.EndsWith('}')) { $text = $text.Substring(1, $text.Length - 2) }
    return $text
}

Invoke-Expression (Get-Functions $catalog)
__BODY__
"""


def run_ps(body: str) -> str:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    catalog_path = str(ROOT / "agent" / "profile-catalog.ps1")
    script = _TEMPLATE.replace("__CATALOG__", catalog_path).replace("__BODY__", body)
    proc = subprocess.run([pwsh, "-NoProfile", "-Command", script], capture_output=True, text=True,
                          timeout=90, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


# --------------------------------------------------------------- dil/sürüm sabitleri

def test_edition_language_and_other_repo_are_correct_and_opposite():
    own_repo, lang, other_repo = edition_values()
    assert own_repo in EXPECTED, f"beklenmeyen depo: {own_repo}"
    assert (lang, other_repo) == EXPECTED[own_repo]
    # Diğer sürüm deposu kendi deposu OLMAMALI.
    assert other_repo != own_repo


def test_language_combo_defaults_to_this_editions_language():
    catalog = read_text("agent/profile-catalog.ps1")
    assert "[void]$langCombo.Items.Add('Türkçe')" in catalog
    assert "[void]$langCombo.Items.Add('English')" in catalog
    # Kapanışa taşınan yerel değişken kullanılır; $script: kapanış içinde görünmez.
    assert "$langCombo.SelectedIndex = if ($editionLang -eq 'en') { 1 } else { 0 }" in catalog
    # 0 = Türkçe, 1 = English; varsayılan bu sürümün dili olmalı.
    _, lang, _ = edition_values()
    expected_index = 1 if lang == "en" else 0
    assert catalog.index(f"$editionLang = $script:MamidLabsEditionLang") < catalog.index("$langCombo.SelectedIndex")
    assert expected_index in (0, 1)


# ------------------------------------------------------------------------ arayüz

def test_language_controls_are_created_and_added_to_the_dialog():
    catalog = read_text("agent/profile-catalog.ps1")
    for name in ("$langLabel", "$langCombo", "$langBtn", "$langDeleteBtn"):
        assert f"{name} = [Windows.Forms" in catalog
    add_range = catalog.split("$dlg.Controls.AddRange")[1]
    for name in ("$langLabel", "$langCombo", "$langBtn", "$langDeleteBtn"):
        assert name in add_range, f"{name} AddRange listesinde degil"


def test_language_download_handler_downloads_to_root_and_writes_record():
    catalog = read_text("agent/profile-catalog.ps1")
    handler = catalog[catalog.index("$langBtn.Add_Click"):]
    handler = handler[:handler.index("$langDeleteBtn.Add_Click")]
    # Kapanış, yerel olarak yakalanan sürüm değişkenlerini kullanır; $script: KULLANMAZ.
    assert "$editionLang" in handler
    assert "$otherEditionRepo" in handler
    assert "$script:MamidLabsEditionLang" not in handler
    assert "$script:OtherEditionRepo" not in handler
    # Akış: sürüm bul → kök altına indir → boyutu doğrula → kaydı yaz → kurulumu başlat.
    assert "Get-EditionRelease $otherEditionRepo" in handler
    assert "Get-EditionDownloadDir" in handler
    assert "MamidLabs_Setup_" in handler
    assert "$release.size" in handler and "Length -ne $release.size" in handler
    assert "Get-EditionRecordPath" in handler and "ConvertTo-Json" in handler
    assert "Start-Process -FilePath $downloadPath" in handler
    assert "$dlg.Close()" in handler
    assert "$mainForm.Close()" in handler
    # Hata/indirme sırasında düğme geri yüklenir.
    assert "$langBtn.Enabled = $false" in handler
    assert "$langBtn.Enabled = $true" in handler


def test_language_delete_handler_removes_package_and_record():
    catalog = read_text("agent/profile-catalog.ps1")
    handler = catalog[catalog.index("$langDeleteBtn.Add_Click"):]
    handler = handler[:handler.index("$dlg.Controls.AddRange")]
    assert "Get-EditionRecord" in handler
    assert "$record.path" in handler
    assert "Remove-Item -LiteralPath $record.path -Force" in handler
    assert "Remove-Item -LiteralPath (Get-EditionRecordPath)" in handler
    assert "$langDeleteBtn.Enabled = $false" in handler
    # Başlangıçta "Sil" yalnızca indirilmiş bir paket kaydı varsa etkindir.
    assert "$langDeleteBtn.Enabled = [bool](Get-EditionRecord)" in catalog


def test_closure_handlers_do_not_read_script_scope_directly():
    """Kapanışlar (GetNewClosure) `$script:` değişkenlerini OKUYAMAZ (boş döner). Silme ve
    bileşen silme koruması canlı `Test-ProfileManagerBusy` üzerinden, aktif profil denetimi
    ise yakalanan `$settings` üzerinden yapılmalıdır."""
    catalog = read_text("agent/profile-catalog.ps1")
    assert "function Test-ProfileManagerBusy" in catalog
    delcomp = catalog[catalog.index("$delCompBtn.Add_Click"):catalog.index("$switchBtn.Add_Click")]
    delete = catalog[catalog.index("$deleteBtn.Add_Click"):catalog.index("$tokenSave.Add_Click")]

    def code_only(block):
        return "\n".join(line for line in block.splitlines() if not line.strip().startswith("#"))

    delcomp_code = code_only(delcomp)
    delete_code = code_only(delete)
    for block in (delcomp_code, delete_code):
        assert "Test-ProfileManagerBusy" in block
        assert "$script:busy" not in block
        assert "$script:imageBusy" not in block
    assert "$script:apiSettings" not in delete_code
    assert "$settings.selectedModel" in delete_code
    # Token kaydetme/silme kapanışları da $script:apiSettings OKUMAZ; yakalanan $settings kullanır.
    token = catalog[catalog.index("$tokenSave.Add_Click"):catalog.index("$updateResetTimer")]
    token_code = code_only(token)
    assert "$script:apiSettings" not in token_code
    assert "$settings" in token_code


def test_profile_manager_busy_helper_reads_live_flags():
    out = run_ps(r"""
function Probe([bool]$b, [bool]$i) {
    $script:busy = $b
    $script:imageBusy = $i
    return [string](Test-ProfileManagerBusy)
}
Write-Output ('t=' + (Probe $true $false))
Write-Output ('f=' + (Probe $false $false))
Write-Output ('img=' + (Probe $false $true))
""")
    assert "t=True" in out
    assert "f=False" in out
    assert "img=True" in out


def test_busy_helper_is_live_not_a_snapshot_inside_a_closure():
    # busy=true iken kapanış oluştur, SONRA false yap: kapanış canlı değeri okumalı (False).
    out = run_ps(r"""
function Make-Closure([bool]$b) {
    $script:busy = $b
    $script:imageBusy = $false
    $sb = { [string](Test-ProfileManagerBusy) }.GetNewClosure()
    $script:busy = $false
    return [string](& $sb)
}
Write-Output ('live=' + (Make-Closure $true))
""")
    assert "live=False" in out


def test_existing_update_button_wiring_is_intact():
    catalog = read_text("agent/profile-catalog.ps1")
    assert "$updateBtn.Add_Click" in catalog
    assert "$updateBtn = [Windows.Forms.Button]::new()" in catalog
    assert "[Windows.Forms.Timer]::new()" in catalog
    assert "$updateResetTimer.Start()" in catalog
    # Kapanış tuzağı: ana form yerel değişkene alınır (tuzak 21).
    assert "$mainForm = $form" in catalog
    assert ".GetNewClosure()" in catalog
    assert "Test-MamidLabsUpdateAvailable" in catalog


def test_downloaded_edition_package_is_cleaned_up_on_uninstall():
    """Paket kök altında (tools\\editions); kaldırıcı 'tools' girdisini siler."""
    uninstall = read_text("scripts/uninstall-mamidlabs.ps1")
    program_entries = uninstall.split("$programEntries = @(")[1].split(")")[0]
    assert "'tools'" in program_entries
    catalog = read_text("agent/profile-catalog.ps1")
    assert "'tools\\editions'" in catalog


# ------------------------------------------------------- Get-EditionRelease (ağ)

def test_edition_release_reports_error_on_network_failure_without_throwing():
    # Geçersiz depo adı: GitHub 404 döndürür (ya da ağ yoksa bağlantı hatası);
    # her iki durumda da fonksiyon İSTİSNA FIRLATMAMALI, @{ error = ... } döndürmeli.
    out = run_ps(r"""
$r = Get-EditionRelease 'workaybarsh/definitely-not-a-real-repo-xyz'
Write-Output ('hasError:' + [bool]$r.error)
""")
    assert "hasError:True" in out


def test_edition_release_reads_the_latest_setup_asset():
    out = run_ps(r"""
function Invoke-RestMethod {
    [pscustomobject]@{ tag_name = 'v1.2.3'; assets = @(
        [pscustomobject]@{ name = 'MamidLabs_Setup.exe'; browser_download_url = 'https://example.com/s.exe'; url = 'https://api.example.com/a'; size = 4321 }) }
}
$r = Get-EditionRelease 'workaybarsh/OtherExt'
Write-Output ('version:' + $r.version)
Write-Output ('url:' + $r.downloadUrl)
Write-Output ('apiUrl:' + $r.apiUrl)
Write-Output ('size:' + $r.size)
Write-Output ('hasError:' + [bool]$r.error)
""")
    assert "version:1.2.3" in out
    assert "url:https://example.com/s.exe" in out
    assert "apiUrl:https://api.example.com/a" in out
    assert "size:4321" in out
    assert "hasError:False" in out


def test_edition_release_missing_setup_asset_reports_error():
    out = run_ps(r"""
function Invoke-RestMethod {
    [pscustomobject]@{ tag_name = 'v1.0.0'; assets = @(
        [pscustomobject]@{ name = 'sources.zip'; url = 'u'; size = 1 }) }
}
$r = Get-EditionRelease 'workaybarsh/OtherExt'
Write-Output ('hasError:' + [bool]$r.error)
Write-Output ('version:' + [string]$r.version)
""")
    assert "hasError:True" in out


# ------------------------------------------- indirme yolu ve kayıt (dosya sistemi)

def _norm(path: str) -> str:
    return path.replace("\\", "/").lower()


def test_edition_download_dir_and_record_path_are_under_the_root(tmp_path):
    root = str(tmp_path)
    out = run_ps(f"""
$root = '{root}'
Write-Output ('dir:' + (Get-EditionDownloadDir))
Write-Output ('rec:' + (Get-EditionRecordPath))
""")
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    got_dir = next(line[len("dir:"):] for line in lines if line.startswith("dir:"))
    got_rec = next(line[len("rec:"):] for line in lines if line.startswith("rec:"))
    assert _norm(got_dir) == _norm(str(tmp_path / "tools" / "editions"))
    assert _norm(got_rec) == _norm(str(tmp_path / "tools" / "editions" / "edition.json"))
    # Kökün altında (kaldırma 'tools' girdisini siler).
    assert _norm(got_dir).startswith(_norm(root))


def test_edition_record_is_null_when_missing(tmp_path):
    out = run_ps(f"""
$root = '{tmp_path}'
Write-Output ('missing:' + [bool](Get-EditionRecord))
""")
    assert "missing:False" in out


def test_edition_record_tolerates_broken_json(tmp_path):
    editions = tmp_path / "tools" / "editions"
    editions.mkdir(parents=True)
    (editions / "edition.json").write_text("{ bu bozuk bir json", encoding="utf-8")
    out = run_ps(f"""
$root = '{tmp_path}'
Write-Output ('broken:' + [bool](Get-EditionRecord))
""")
    assert "broken:False" in out


def test_edition_record_reads_a_valid_record(tmp_path):
    editions = tmp_path / "tools" / "editions"
    editions.mkdir(parents=True)
    (editions / "edition.json").write_text(
        json.dumps({"lang": "en", "version": "1.0.0", "path": "C:/x/MamidLabs_Setup_en.exe",
                    "installedAt": "2026-10-12T00:00:00.0000000"}),
        encoding="utf-8")
    out = run_ps(f"""
$root = '{tmp_path}'
$r = Get-EditionRecord
Write-Output ('lang:' + [string]$r.lang)
Write-Output ('version:' + [string]$r.version)
Write-Output ('path:' + [string]$r.path)
""")
    assert "lang:en" in out
    assert "version:1.0.0" in out
    assert "path:C:/x/MamidLabs_Setup_en.exe" in out


# ------------------------------------- kapanış (GetNewClosure) kapsam davranışı

def test_language_handler_reads_captured_locals_inside_the_closure():
    """Gerçek `$langBtn.Add_Click` gövdesi kapanış olarak çalıştırılır: sürüm dili ve
    diğer sürüm deposu YEREL olarak yakalanmışsa doğru davranır. $script: ile
    yazılmış olsaydı kapanış içinde boş dönerdi (bilinen tuzak)."""
    _, lang, _ = edition_values()
    own_index = 1 if lang == "en" else 0
    other_index = 0 if lang == "en" else 1
    body = f"""
function Get-EditionRelease([string]$r) {{ [void]$script:released.Add($r); return @{{ error = 'boom' }} }}
$handlerBody = Get-ClosureBody $catalog '$langBtn' 'Click'
function Invoke-Scenario([int]$index, [string]$lang, [string]$repo) {{
    $script:released = [Collections.ArrayList]::new()
    $editionLang = $lang
    $otherEditionRepo = $repo
    $status = @{{ Text = '' }}
    $langCombo = @{{ SelectedIndex = $index }}
    $langBtn = @{{ Text = 'L'; Enabled = $true }}
    $langDeleteBtn = @{{ Enabled = $false }}
    $dlg = [pscustomobject]@{{ refreshed = 0 }}
    $dlg | Add-Member -MemberType ScriptMethod -Name Refresh -Value {{ }}
    $dlg | Add-Member -MemberType ScriptMethod -Name Close -Value {{ }}
    $mainForm = $null
    $handler = ([scriptblock]::Create($handlerBody)).GetNewClosure()
    & $handler
    Write-Output ('released=' + (@($script:released) -join ','))
    Write-Output ('count=' + @($script:released).Count)
    Write-Output ('statusEmpty=' + [string]([string]::IsNullOrEmpty([string]$status.Text)))
}}
Write-Output 'SCENARIO-SAME'
Invoke-Scenario {own_index} '{lang}' 'workaybarsh/OtherExt'
Write-Output 'SCENARIO-OTHER'
Invoke-Scenario {other_index} '{lang}' 'workaybarsh/OtherExt'
"""
    out = run_ps(body)
    same = out.split("SCENARIO-SAME", 1)[1].split("SCENARIO-OTHER", 1)[0]
    other = out.split("SCENARIO-OTHER", 1)[1]

    def field(chunk, name):
        return re.search(rf"{name}=(.*)", chunk).group(1).strip()

    # Bu sürümün kendi dili seçilince "zaten kurulu": Get-EditionRelease HİÇ çağrılmaz.
    assert field(same, "count") == "0", out
    assert field(same, "statusEmpty") == "False", out
    # Diğer dil seçilince indirme akışı DIĞER sürüm deposuyla başlar (yerel yakalanır).
    assert field(other, "released") == "workaybarsh/OtherExt", out
    assert field(other, "count") == "1", out
    assert field(other, "statusEmpty") == "False", out
