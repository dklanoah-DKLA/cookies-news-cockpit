from __future__ import annotations

import re
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def test_runtime_and_build_dependencies_are_declared() -> None:
    project = tomllib.loads(read("pyproject.toml"))["project"]
    runtime_names = {
        re.split(r"[<>=!~\[]", item, maxsplit=1)[0].lower()
        for item in project["dependencies"]
    }
    dev_names = {
        re.split(r"[<>=!~\[]", item, maxsplit=1)[0].lower()
        for item in project["optional-dependencies"]["dev"]
    }

    assert project["requires-python"] == ">=3.12,<3.13"
    assert {
        "beautifulsoup4",
        "fastapi",
        "feedparser",
        "httpx",
        "keyring",
        "platformdirs",
        "pydantic",
        "uvicorn",
    } <= runtime_names
    assert {"pyinstaller", "pytest"} <= dev_names


def test_pyinstaller_bundle_contract_targets_intel_sonoma() -> None:
    spec = read("packaging/CookiesNewsCockpit.spec")

    assert "PROJECT_ROOT = Path(SPECPATH).resolve().parent\n" in spec
    assert "Path(SPECPATH).resolve().parent.parent" not in spec
    assert 'target_arch="x86_64"' in spec
    assert 'bundle_identifier="com.cookies.newscockpit"' in spec
    assert '"LSMinimumSystemVersion": "14.0"' in spec
    assert 'APP_NAME = "Cookies News Cockpit"' in spec
    assert 'icon=str(ICON_PATH)' in spec
    assert "collect_data_files" in spec
    assert "BUNDLE(" in spec


def test_packaged_entrypoint_uses_an_absolute_package_import() -> None:
    entrypoint = read("src/cookies_news_cockpit/__main__.py")

    assert "from cookies_news_cockpit.launcher import main" in entrypoint
    assert "from .launcher" not in entrypoint


def test_build_script_validates_architecture_and_ad_hoc_signature() -> None:
    script = read("packaging/build_macos.sh")

    assert '"$(uname -m)" != "x86_64"' in script
    assert "codesign --force --deep --sign - --timestamp=none" in script
    assert "codesign --verify --deep --strict" in script
    assert 'xattr -cr "${APP_PATH}"' in script
    assert "lipo -archs" in script
    assert 'MACOSX_DEPLOYMENT_TARGET="14.0"' in script


def test_dmg_script_emits_transfer_fallback_and_checksums() -> None:
    script = read("packaging/package_dmg.sh")

    assert "hdiutil create" in script
    assert "-format UDZO" in script
    assert "ditto -c -k --keepParent" in script
    assert ".dmg.zip" not in script  # ZIP_PATH is derived from the complete DMG filename.
    assert "shasum -a 256" in script
    assert "hdiutil verify" in script
    assert 'ln -s /Applications "${STAGING_DIR}/Applications"' in script


def test_icon_generator_builds_every_required_icns_size() -> None:
    script = read("packaging/generate_icon.py")

    for filename in (
        "icon_16x16.png",
        "icon_16x16@2x.png",
        "icon_32x32.png",
        "icon_32x32@2x.png",
        "icon_128x128.png",
        "icon_128x128@2x.png",
        "icon_256x256.png",
        "icon_256x256@2x.png",
        "icon_512x512.png",
        "icon_512x512@2x.png",
    ):
        assert filename in script
    assert '"iconutil", "-c", "icns"' in script


def test_workflow_is_manual_or_tagged_and_uses_official_intel_runner() -> None:
    workflow = read(".github/workflows/build-macos-intel.yml")

    assert "workflow_dispatch:" in workflow
    assert 'tags:\n      - "v*"' in workflow
    assert "runs-on: macos-15-intel" in workflow
    assert 'python-version: "3.12"' in workflow
    assert "bash packaging/build_macos.sh" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "release/*.dmg.zip" in workflow
    assert "SHA256SUMS.txt" in workflow
    assert "APPLE_ID" not in workflow
    assert "NOTARY" not in workflow.upper()


def test_bundled_fonts_ship_with_their_license() -> None:
    pyproject = read("pyproject.toml")
    license_text = read("src/cookies_news_cockpit/licenses/FONT-LICENSES.txt")

    assert '"licenses/*"' in pyproject
    assert "Bricolage Grotesque Project Authors" in license_text
    assert 'Reserved Font Name "Plex"' in license_text
    assert "SIL OPEN FONT LICENSE Version 1.1" in license_text
