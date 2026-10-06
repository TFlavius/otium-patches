# Some screenshots at first

![About](/img/1.png)
![HN](/img/2.png)
![ES1015 support](/img/3.png)

# Applying an Otium source patch

This bundle reconstructs one Otium source snapshot from the original source files you supply. It does not contain the complete original sources, browser binaries or development Git history. It does not download original sources or dependencies. This remains a local prototype requiring content and rights review before publication.

## Prerequisites

- Python 3.10 or newer, available as `py` or `python` on Windows, or `python3` on Linux. Git is not needed to reconstruct the browser sources; it is required for the optional dependency download after application.
- The original source tree required by this bundle. Run the diagnostic command below to see the baseline commit and target commit. Raw bytes must match that baseline, except for a verified CRLF checkout of text files: the tool accepts CRLF-to-LF bytes only when their complete size and SHA-256 match the original. It does not rewrite your files, change Git settings, normalize actual symlink targets or data classified as binary by Git-style text heuristics, or accept a different revision or substantive edits.
- A new destination directory that does not exist yet, outside both the original tree and this bundle. Allow space for the complete reconstructed source tree.

Keep the bundle intact, including `payloads/`, `manifest.json`, `otium_patch.py` and both launchers. The Python script and launchers sit directly beside this README; there is no nested tools directory. Use a bundle from a trusted source: hashes detect corrupted data but do not authenticate its publisher.

These are the generated bundle's instructions, not instructions for creating a bundle in the development repository. Its launchers select the manifest beside their Python script automatically, even if invoked by an absolute path from another working directory. `--bundle <directory>` overrides that selection. Development-repository launchers under `tools/otium-patch/` instead require an explicit `--bundle` for application or source diagnostics; they do not search your working directory for bundles.

## Windows

Open PowerShell in this bundle's directory. First inspect it and check the original files without writing an output tree:

```powershell
.\patch.bat --doctor --source "D:\sources\original-presto"
```

Then reconstruct the target sources into a new directory:

```powershell
.\patch.bat --apply --source "D:\sources\original-presto" --destination "D:\sources\otium-src"
```

The launcher finds Python automatically. If the target contains symbolic links, Windows must permit their creation, for example through Developer Mode. A symlink creation failure is reported and the newly created incomplete destination is removed. POSIX executable bits cannot be represented as ordinary Windows file permissions.

Both `--source="path"` and `--source "path"` are supported. Quoted directory paths may end in a backslash: the Windows launcher re-quotes its arguments for Python so the closing quote does not get escaped and swallow the next option. Older bundles created before this launcher fix require an updated `patch.bat`, or paths without a trailing backslash as a workaround.

## Linux

Open a terminal in this bundle's directory:

```sh
sh ./patch.sh --doctor --source "/path/to/original-presto"
sh ./patch.sh --apply --source "/path/to/original-presto" --destination "/path/to/otium-src"
```

On a Windows host, use a source tree and destination on WSL's native Linux filesystem when preparing a Linux build. The same arguments can be passed directly to `python3 -B otium_patch.py` if needed.

## What application does

By default, the tool validates the required original files, payload hashes, paths and complete reconstructed file hashes before creating the destination. It writes only the target snapshot, so files removed in this release are absent without deleting anything from your original sources. It never overwrites an existing destination, even an empty one, and never modifies the original tree. On a normal caught failure after output creation, it removes only the incomplete directory it just created. An abrupt process termination can leave that directory behind; inspect it and choose a new destination before retrying.

`--doctor --source` performs those checks without creating the destination or applying the patch. `--apply --source --destination` performs the checks and writes the result. The source is always the stated original baseline; the destination is always a new directory for the complete target snapshot. A `Verified CRLF-to-LF checkout conversion` message identifies a size-and-hash-proven equivalent of the original bytes, not a change made to your checkout. Other checkout filters, bare-CR text conversion and changes to binary data are not supported.

Successful application prints the destination and file count. If the manifest contains submodule pins, their paths and exact commits are listed separately: those contents are not included in the bundle. The applicator prints complete commands to restore them and offers to run them after confirmation, as described below. The reconstructed tree has no `.git` directory, so `git submodule update --init` from the project's general clone instructions does not work there. Follow the reconstructed project's build instructions after supplying the required dependencies.

Each bundle is cumulative from its stated original baseline. Do not apply it to a previous Otium release. To use another release, start from the same original sources and reconstruct a separate destination.

## Restore Dragonfly before building

The browser currently requires the separately distributed Dragonfly developer-tools client under `tools/dragonfly`. After application, complete commands are printed using the URL in the reconstructed `.gitmodules` and the exact commit recorded by this bundle. Copy those commands into PowerShell on Windows or a POSIX shell on Linux; their absolute paths let you run them from any directory. Alternatively answer `y` or `yes` to the terminal prompt to let the applicator run them. The default answer is no; non-interactive runs never prompt or download. Install Git first if it is not on PATH. `--doctor` remains a no-write diagnostic.

For manual setup from the reconstructed browser's directory, use the following commands, replacing `RECORDED_COMMIT` with the exact commit printed for `tools/dragonfly` (also available through the bundle's `--doctor`). Do not use the repository's current branch tip instead of the recorded commit:

```text
git clone --no-checkout -- https://github.com/TFlavius/dragonfly.git tools/dragonfly
git -C tools/dragonfly checkout --detach "RECORDED_COMMIT"
```

This creates a Git checkout for Dragonfly only; it does not require or recreate the browser's Git history. Only HTTPS and local `file://` dependency URLs are supported by the confirmation helper; other configurations require manual setup. Existing dependency directories are never overwritten. A clone or checkout failure returns nonzero and removes only the helper's newly created incomplete dependency directory; the reconstructed browser remains intact, so you can retry the manual commands without applying the entire patch again. If a dependency directory already exists, inspect it yourself rather than deleting it blindly.

## Unsafe application to mismatching files

Add `--ignore` only if you deliberately accept an unverified, possibly corrupt result. It allows mismatched source files and target hash mismatches caused by their COPY operations, reporting warnings instead of claiming verification. It does not relocate COPY offsets to accommodate content changes. Text classified as CRLF text is converted to LF in memory without claiming a baseline match; binary data and actual symlink targets are not converted. Missing files, out-of-range COPY operations, corrupt payloads, unsafe paths/links, unrelated target hash failures and existing destinations still fail. Your source tree is never edited. Prefer obtaining the matching archive or asking the bundle creator to regenerate against the correct baseline.

```powershell
.\patch.bat --apply --ignore --source "D:\sources\original-presto" --destination "D:\sources\unverified-otium-src"
```

```sh
sh ./patch.sh --apply --ignore --source "/path/to/original-presto" --destination "/path/to/unverified-otium-src"
```

`--doctor --ignore --source <original-tree>` performs the same unsafe preflight without writing a destination and reports that verification is not guaranteed. `--ignore` is not accepted for bundle creation or author-side Git verification. An old bundle keeps its recorded baseline; a newer applicator cannot safely change that baseline without regenerating the bundle recipes.

## Diagnostics

From the bundle root, `.\patch.bat --doctor` or `sh ./patch.sh --doctor` reports the Python version, selected bundle, baseline and target commits, file count and submodule pins. Add `--source <original-tree>` for full no-write reconstruction verification. `--help` lists all options. Missing-argument errors name only the missing inputs and identify a selected bundle when available. If source verification fails, the message distinguishes a size mismatch from a hash mismatch and gives expected and observed sizes, the expected SHA-256, and the observed SHA-256 when a bounded read was possible. Confirm the source revision and bytes before considering the unsafe override described above. All commands return a nonzero exit code on failure; successful `--ignore` application does not establish correctness.
