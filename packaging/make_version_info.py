"""Writes packaging/version_info.txt (Windows file properties for SubScout.exe) from APP_VERSION."""
import pathlib
import re

root = pathlib.Path(__file__).resolve().parent.parent
version = re.search(r'APP_VERSION = "([^"]+)"', (root / "SubScout.pyw").read_text(encoding="utf-8")).group(1)
parts = (version.split(".") + ["0", "0", "0"])[:4]
nums = ", ".join(str(int(p)) for p in parts)
dotted = ".".join(parts)

(root / "packaging" / "version_info.txt").write_text(f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(filevers=({nums}), prodvers=({nums}), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'ilukezippo'),
      StringStruct('FileDescription', 'SubScout - English & Arabic subtitle downloader'),
      StringStruct('FileVersion', '{dotted}'),
      StringStruct('InternalName', 'SubScout'),
      StringStruct('LegalCopyright', 'ilukezippo'),
      StringStruct('OriginalFilename', 'SubScout.exe'),
      StringStruct('ProductName', 'SubScout'),
      StringStruct('ProductVersion', '{dotted}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")
print(version)
