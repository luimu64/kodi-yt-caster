#!/usr/bin/env python3
"""Generate Kodi repository index (addons.xml + addons.xml.md5) from dist/*.zip.

Scans every addon zip in dist/, extracts each addon.xml, and builds a
repository addons.xml containing one <addon> block per zip version with
download URL + checksum. Writes addons.xml.md5 alongside.
"""
from __future__ import annotations

import hashlib
import sys
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

REPO_URL = "https://luimu64.github.io/kodi-yt-caster"


def sha1_of_file(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def addon_xml_from_zip(zpath: Path) -> str:
    with zipfile.ZipFile(zpath) as z:
        names = [n for n in z.namelist() if n.count("/") == 1 and n.endswith("/addon.xml")]
        if not names:
            raise ValueError(f"no root addon.xml in {zpath}")
        return z.read(names[0]).decode("utf-8")


def build_addons_xml(dist_dir: Path, base_url: str) -> str:
    zips = sorted(dist_dir.glob("*.zip"))
    if not zips:
        print(f"no zips in {dist_dir}", file=sys.stderr)
        return ""

    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', "<addons>"]
    seen = set()
    for zp in zips:
        xml_text = addon_xml_from_zip(zp)
        root = ET.fromstring(xml_text)
        aid = root.get("id") or root.get("point", "")
        ver = root.get("version")
        key = (aid, ver)
        if key in seen:  # dedupe rebuilds
            continue
        seen.add(key)

        # inject extension point with download URL + checksum
        url = f"{base_url}/{urllib.parse.quote(zp.name)}"
        ext = ET.Element("extension", {
            "point": "kodi.addon.repository",
            "name": base_url,
        })
        info = ET.SubElement(ext, "info", compressed="false")
        info.text = url
        checksum = ET.SubElement(ext, "checksum")
        checksum.text = f"{sha1_of_file(zp)}"
        datadir = ET.SubElement(ext, "datadir", zip="true")
        datadir.text = f"{base_url}/"
        # serialize the addon node with repository extension appended
        parent = ET.Element("addon", root.attrib)
        for child in root:
            parent.append(child)
        parent.append(ext)
        out.append(ET.tostring(parent, encoding="unicode"))
    out.append("</addons>")
    return "\n".join(out)


def main() -> int:
    base = Path(__file__).resolve().parent
    dist = base / "dist"
    dist.mkdir(exist_ok=True)
    xml = build_addons_xml(dist, REPO_URL)
    if not xml:
        return 1
    (dist / "addons.xml").write_text(xml, encoding="utf-8")
    md5 = hashlib.md5(xml.encode("utf-8")).hexdigest()
    (dist / "addons.xml.md5").write_text(md5 + "\n", encoding="ascii")
    print(f"wrote {dist/'addons.xml'} ({md5})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
