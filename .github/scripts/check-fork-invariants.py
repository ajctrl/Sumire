"""Fail updates that drop the fork's app identity or clipboard IPC integration."""
from pathlib import Path
import xml.etree.ElementTree as ET


def main():
    root = Path(__file__).resolve().parents[2]
    gradle = (root / "app/build.gradle").read_text()
    assert 'applicationId "com.example.sumire"' in gradle, "Fork application ID changed"
    assert 'buildConfigField "String", "SUMIRE_SYNC_PACKAGE_ID"' in gradle
    manifest = ET.parse(root / "app/src/main/AndroidManifest.xml").getroot()
    android = "{http://schemas.android.com/apk/res/android}"
    permission = "${applicationId}.permission.CLIPBOARD_SYNC"
    assert any(p.get(android + "name") == permission and
               p.get(android + "protectionLevel") == "signature"
               for p in manifest.findall("permission")), "Signature permission lost"
    provider = next(p for p in manifest.findall("application/provider")
                    if p.get(android + "name") == ".syncbridge.ClipboardSyncProvider")
    for key, expected in {"authorities": "${applicationId}.syncbridge.clipboard",
                          "exported": "true", "grantUriPermissions": "false",
                          "readPermission": permission, "writePermission": permission}.items():
        assert provider.get(android + key) == expected, f"Provider {key} changed"
    source = root / "app/src/main/java/com/kazumaproject/markdownhelperkeyboard"
    service = (source / "ime_service/IMEService.kt").read_text()
    assert "ClipboardSyncNotifier.notifyClipboardChanged(this@IMEService)" in service
    assert "val insertedId = clipboardHistoryRepository.insertFromClipboard(newItem)" in service
    assert "insertedId != null && insertedId > 0L" in service
    for name in ("ClipboardSyncContract", "ClipboardSyncEntryPoint", "ClipboardSyncNotifier",
                 "ClipboardSyncProvider", "DatabaseInstanceIdStore"):
        assert (source / f"syncbridge/{name}.kt").is_file(), f"Missing {name}"
    print("Fork identity and clipboard integration checks passed")


if __name__ == "__main__":
    main()
