"""Adaptive local helpers for the neoHarness Office utility runtime."""

from .inspectors import compare_files, inspect_file
from .ooxml import (
    replace_text,
    replace_text_with_field,
    update_content_control,
    update_xlsx_cells,
)
from .quality import artifact_provenance, policy_identity, qualify_artifact

__all__ = [
    "compare_files",
    "inspect_file",
    "replace_text",
    "replace_text_with_field",
    "update_content_control",
    "update_xlsx_cells",
    "artifact_provenance",
    "policy_identity",
    "qualify_artifact",
]
__version__ = "9.3.3-nh1"
