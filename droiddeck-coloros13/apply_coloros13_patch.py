#!/usr/bin/env python3
"""Patch DroidDeck 0.4.0 for a side-by-side ColorOS 13 test build.

Never touches the Android device or original installed APK.
Run at repository root: python3 /path/to/apply_coloros13_patch.py --repo .
Use --check to validate applicability without writing files.
"""
from __future__ import annotations

import argparse
from pathlib import Path


def replace_exact(source: str, old: str, new: str, filename: str, description: str) -> str:
    if new in source:
        raise RuntimeError(f"{filename}: {description} seems already applied; refusing duplicate patch")
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{filename}: {description}: expected 1 matching anchor, got {count}")
    return source.replace(old, new, 1)


def edits() -> dict[str, list[tuple[str, str, str]]]:
    gradle_path = "app/build.gradle"
    main_path = "app/src/main/java/com/droiddeck/launcher/MainActivity.kt"
    screen_path = "app/src/main/java/com/droiddeck/launcher/ui/FrontEndScreen.kt"
    page_path = "app/src/main/java/com/droiddeck/launcher/ui/PhantomProcessGatePage.kt"
    return {
        gradle_path: [(
            "        debug {\n            minifyEnabled false\n            signingConfig signingConfigs.testkey",
            "        debug {\n            // A separate package: cannot overwrite the official app or its data.\n"
            '            applicationIdSuffix ".colorosfix"\n'
            '            versionNameSuffix "-colorosfix"\n'
            "            minifyEnabled false\n            signingConfig signingConfigs.testkey",
            "side-by-side debug application ID",
        )],
        main_path: [
            (
                "    private var showPhantomGate by mutableStateOf(false)",
                "    private var showPhantomGate by mutableStateOf(false)\n"
                "    // Ephemeral: re-confirm after the app process is restarted (including reboot).\n"
                "    private var manualPhantomGateBypass = false",
                "ephemeral confirmation state",
            ),
            (
                "                        onDismissPhantomGate = { showPhantomGate = false },",
                "                        onDismissPhantomGate = { showPhantomGate = false },\n"
                "                        onManuallyVerifiedPhantomLimit = {\n"
                "                            // Only Android 13 ROMs with an unset/unreadable toggle.\n"
                "                            if (Build.VERSION.SDK_INT == Build.VERSION_CODES.TIRAMISU &&\n"
                "                                (phantomProcessStatus == PhantomProcessStatus.UNSET ||\n"
                "                                    phantomProcessStatus == PhantomProcessStatus.UNREADABLE)) {\n"
                "                                manualPhantomGateBypass = true\n"
                "                                showPhantomGate = false\n"
                "                            }\n"
                "                        },",
                "manual confirmation callback",
            ),
            (
                "        if (steamSession && PhantomProcessLimit.blocksSteam(phantomProcessStatus)) {\n"
                "            showPhantomGate = true\n            return false\n        }",
                "        val manualBypassAllowed = Build.VERSION.SDK_INT == Build.VERSION_CODES.TIRAMISU &&\n"
                "            manualPhantomGateBypass &&\n"
                "            (phantomProcessStatus == PhantomProcessStatus.UNSET ||\n"
                "                phantomProcessStatus == PhantomProcessStatus.UNREADABLE)\n"
                "        if (steamSession && PhantomProcessLimit.blocksSteam(phantomProcessStatus) && !manualBypassAllowed) {\n"
                "            showPhantomGate = true\n            return false\n        }",
                "Steam gate limited override",
            ),
        ],
        screen_path: [
            (
                "    val onDismissPhantomGate: () -> Unit = {},",
                "    val onDismissPhantomGate: () -> Unit = {},\n"
                "    val onManuallyVerifiedPhantomLimit: () -> Unit = {},",
                "FrontEndActions callback",
            ),
            (
                "                    onCopyCommand = { a.onCopyPhantomCommand(false) },\n"
                "                    onOpenNotificationSettings = a.onOpenNotificationSettings,",
                "                    onCopyCommand = { a.onCopyPhantomCommand(false) },\n"
                "                    onManualVerified = a.onManuallyVerifiedPhantomLimit,\n"
                "                    onOpenNotificationSettings = a.onOpenNotificationSettings,",
                "wire gate callback into screen",
            ),
        ],
        page_path: [
            (
                "    onCopyCommand: () -> Unit,\n    onOpenNotificationSettings: () -> Unit,",
                "    onCopyCommand: () -> Unit,\n    onManualVerified: () -> Unit,\n"
                "    onOpenNotificationSettings: () -> Unit,",
                "add gate callback parameter",
            ),
            (
                "                    OtherWays(hasToggle, compact, onOpenDeveloperOptions, onFixWithWirelessDebugging, onEnterAddressManually, onCopyCommand)",
                "                    OtherWays(status, hasToggle, compact, onOpenDeveloperOptions, onFixWithWirelessDebugging, onEnterAddressManually, onCopyCommand, onManualVerified)",
                "pass status and callback to OtherWays",
            ),
            (
                "private fun OtherWays(\n    hasToggle: Boolean,",
                "private fun OtherWays(\n    status: PhantomProcessStatus,\n    hasToggle: Boolean,",
                "OtherWays status parameter",
            ),
            (
                "    onEnterAddressManually: () -> Unit,\n    onCopyCommand: () -> Unit,\n) {\n    var open by rememberSaveable { mutableStateOf(false) }",
                "    onEnterAddressManually: () -> Unit,\n    onCopyCommand: () -> Unit,\n"
                "    onManualVerified: () -> Unit,\n) {\n    var open by rememberSaveable { mutableStateOf(false) }\n"
                "    var manualConfirmOpen by rememberSaveable { mutableStateOf(false) }",
                "OtherWays manual confirmation UI state",
            ),
            (
                "            SecondaryButton(stringResource(R.string.gate_copy_command), compact = true, onClick = onCopyCommand)\n"
                "        }\n    }\n}\n\n@Composable\nprivate fun Body",
                "            SecondaryButton(stringResource(R.string.gate_copy_command), compact = true, onClick = onCopyCommand)\n"
                "            if (Build.VERSION.SDK_INT == Build.VERSION_CODES.TIRAMISU &&\n"
                "                (status == PhantomProcessStatus.UNSET || status == PhantomProcessStatus.UNREADABLE)) {\n"
                "                Body(\"ColorOS 13 workaround: verify with ADB that dumpsys activity settings reports max_phantom_processes=2147483647.\")\n"
                "                SecondaryButton(\"I verified the limit using ADB\", compact = true, onClick = { manualConfirmOpen = true })\n"
                "            }\n"
                "        }\n    }\n"
                "    if (manualConfirmOpen) {\n"
                "        AlertDialog(\n"
                "            onDismissRequest = { manualConfirmOpen = false },\n"
                "            title = { Text(\"Manual process-limit confirmation\") },\n"
                "            text = { Text(\"Continue only after confirming with USB ADB that max_phantom_processes=2147483647 is active. DroidDeck cannot verify this ROM setting. This bypass lasts only until DroidDeck closes; it does not change Android settings.\") },\n"
                "            confirmButton = { TextButton(onClick = { manualConfirmOpen = false; onManualVerified() }) { Text(\"Allow Steam launch\") } },\n"
                "            dismissButton = { TextButton(onClick = { manualConfirmOpen = false }) { Text(\"Cancel\") } },\n"
                "        )\n"
                "    }\n"
                "}\n\n@Composable\nprivate fun Body",
                "confirmation dialog and warning",
            ),
        ],
    }


def patch_repository(repo: Path, check: bool) -> list[str]:
    planned: dict[Path, str] = {}
    reports: list[str] = []
    for relative, steps in edits().items():
        path = repo / relative
        if not path.is_file():
            raise RuntimeError(f"Missing expected source file: {path}")
        text = path.read_text(encoding="utf-8")
        for old, new, description in steps:
            text = replace_exact(text, old, new, relative, description)
            reports.append(f"PASS {relative}: {description}")
        planned[path] = text
    if not check:
        for path, value in planned.items():
            path.write_text(value, encoding="utf-8", newline="\n")
    return reports


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", type=Path, default=Path.cwd(), help="DroidDeck checkout root")
    p.add_argument("--check", action="store_true", help="Check every anchor without editing source")
    args = p.parse_args()
    try:
        reports = patch_repository(args.repo.resolve(), args.check)
    except (OSError, UnicodeError, RuntimeError) as exc:
        p.exit(1, f"ERROR: {exc}\nNo APK was modified; inspect repository state before retrying.\n")
    print("\n".join(reports))
    print("CHECK ONLY: no files changed" if args.check else "PATCH APPLIED: source modified; APK not built")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
