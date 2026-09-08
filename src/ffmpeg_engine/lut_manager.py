"""
LUT Manager for Blackmagic RAW Video Converter.
Resolves, validates, and discovers 3D .cube LUT files.
"""

import os
from pathlib import Path
from typing import List, Optional, Dict
from src.common.logger import setup_logger

logger = setup_logger("lut_manager")


class LutManager:
    """Discovers and resolves .cube 3D LUT files."""

    def __init__(self, custom_lut_dirs: Optional[List[Path]] = None):
        self.lut_dirs: List[Path] = []

        # 1. Project bundled LUTs directory
        bundled_dir = Path(__file__).parent.parent.parent / "assets" / "luts"
        if bundled_dir.is_dir():
            self.lut_dirs.append(bundled_dir.resolve())

        # 2. Workspace root assets/luts
        workspace_dir = Path.cwd() / "assets" / "luts"
        if workspace_dir.is_dir() and workspace_dir.resolve() not in self.lut_dirs:
            self.lut_dirs.append(workspace_dir.resolve())

        # 3. System / DaVinci Resolve legacy LUT paths (optional fallback)
        system_bmd_lut = Path("/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT/Blackmagic Design")
        if system_bmd_lut.is_dir() and system_bmd_lut.resolve() not in self.lut_dirs:
            self.lut_dirs.append(system_bmd_lut.resolve())

        # 4. Custom user directories
        if custom_lut_dirs:
            for d in custom_lut_dirs:
                if d.is_dir() and d.resolve() not in self.lut_dirs:
                    self.lut_dirs.append(d.resolve())

    def list_available_luts(self) -> List[Dict[str, str]]:
        """Returns a list of all available LUTs with names and paths."""
        results = []
        seen_names = set()

        for lut_dir in self.lut_dirs:
            for lut_file in lut_dir.glob("*.cube"):
                if lut_file.name not in seen_names:
                    seen_names.add(lut_file.name)
                    results.append({
                        "name": lut_file.stem,
                        "filename": lut_file.name,
                        "path": str(lut_file.resolve())
                    })

        return sorted(results, key=lambda x: x["name"])

    def resolve_lut(self, lut_identifier: str, fallback_identifier: Optional[str] = None) -> Optional[Path]:
        """
        Resolves a LUT path from a full path, relative path, or LUT name.
        """
        if not lut_identifier or lut_identifier.lower() == "none":
            return None

        # Check if direct absolute or relative file path exists
        candidate = Path(lut_identifier)
        if candidate.is_file():
            return candidate.resolve()

        clean_name = candidate.name
        # Remove extension if provided
        stem = Path(clean_name).stem

        # Try matching in searched directories
        for lut_dir in self.lut_dirs:
            # 1. Exact filename match
            p = lut_dir / clean_name
            if p.is_file():
                return p.resolve()

            # 2. Name with .cube
            p = lut_dir / f"{clean_name}.cube"
            if p.is_file():
                return p.resolve()

            # 3. Case-insensitive or normalized match
            for existing in lut_dir.glob("*.cube"):
                if existing.stem.lower() == stem.lower() or existing.name.lower() == clean_name.lower():
                    return existing.resolve()
                # Partial match for common shorthand (e.g. "Gen 5 to Extended Video" -> "Blackmagic Gen 5 Film to Extended Video.cube")
                if "gen 5" in stem.lower() and "extended video" in stem.lower():
                    if "gen 5" in existing.stem.lower() and "extended video" in existing.stem.lower():
                        return existing.resolve()

        # If primary failed, try fallback
        if fallback_identifier:
            logger.warning(f"LUT '{lut_identifier}' not found. Trying fallback '{fallback_identifier}'")
            return self.resolve_lut(fallback_identifier, fallback_identifier=None)

        logger.warning(f"Could not resolve 3D LUT for identifier: '{lut_identifier}'")
        return None
