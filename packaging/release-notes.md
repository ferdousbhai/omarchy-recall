Recall for Omarchy v0.4.3 improves installation safety and introduces signed releases.

- Release bundles for x86_64 and aarch64, plus install.sh, are signed with keyless Sigstore through this repository’s GitHub release workflow. Verification instructions are in README.md.
- The installer lists packages and asks before installing them. Use --yes or RECALL_YES=1 for unattended installation.
- Desktop setup backs up keybindings and restores them when Hyprland reports configuration errors. Set RECALL_NO_KEYBINDING=1 to skip adding the shortcut.
- Desktop entries use the fork’s app ID; setup removes entries created under the previous ID.
- Invalid configuration is rejected before saving. Reader panics are recorded as failed batches so indexing can continue.
- Rust 1.89 is the minimum supported version, with CI on both 1.89 and stable.

Existing indexes, models, and selected folders are preserved. OCR continues to use system Tesseract; CPU fallback and Vulkan acceleration remain available on both architectures.

Install: `curl -fsSL https://ferdousbhai.com/recall/install.sh | bash`

Based on pdstn12/Recall (MIT), inspired by ARahim3/DigUp.
