"""Adaptive local helpers for the neoHarness Office utility runtime."""

from .inspectors import compare_files, inspect_file
from .ooxml import (
    format_xlsx,
    replace_text,
    replace_text_with_field,
    update_content_control,
    update_xlsx_cells,
)
from .quality import artifact_provenance, policy_identity, qualify_artifact

__all__ = [
    "artifact_provenance",
    "compare_files",
    "format_xlsx",
    "inspect_file",
    "policy_identity",
    "qualify_artifact",
    "replace_text",
    "replace_text_with_field",
    "update_content_control",
    "update_xlsx_cells",
]
__version__ = "9.3.3-nh1"
