# IDrive Backup Helper

A browser-driven CLI for downloading folders from IDrive's web interface. It controls a Playwright-managed Chromium browser to navigate IDrive, list files, and download them to a local destination.

## Before you start

- Install [uv](https://docs.astral.sh/uv/getting-started/installation/):
  - macOS/Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
  - Windows PowerShell: `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`
- Install dependencies and the managed Chromium browser:

```sh
uv sync
uv run poe browser-setup
```

## Typical workflow

### 1. Authenticate

Open a headed browser and log in to IDrive. The session is saved so you do not need to repeat this step.

```sh
uv run main auth
```

### 2. Find the folder URL

Use the `browse` command to open an authenticated IDrive browser for manual navigation. Browse to the folder you want to download and copy its URL from the address bar.

```sh
uv run main browse
```

The URL you are looking for looks like:

```text
https://www.idrive.com/idrive/home/<device>_<device_id>/<drive>/path/to/folder
```

### 3. Download a folder

```sh
uv run main download-folder \
  --headed \
  --url "https://www.idrive.com/idrive/home/<device>_<device_id>/<drive>/path/to/folder" \
  --to "/media/<user>/<media>/<device>/path/to/folder"
```

`--headed` keeps the browser window visible while downloading. Omit it for a headless run once you are confident the session and URL are correct.

#### Download options

| Flag | Default | Description |
| ---- | ------- | ----------- |
| `--url URL` | required | IDrive folder URL |
| `--to PATH` | required | Local destination directory |
| `--headed` | off | Show the browser window |
| `--timeout-ms N` | — | Playwright timeout for page operations |
| `--cooldown-ms N` | — | Pause between file download interactions |
| `--overwrite {skip,replace,fail}` | `skip` | How to handle already-existing files |
| `--no-folder-cache` | off | Disable cached folder listings |
| `--no-resume-logs` | off | Disable resume indexing from prior manifests |
| `--exclude GLOB` | — | Skip files matching `GLOB`; repeatable, see below |
| `--browser-debug-url URL` | — | Attach to a different Chromium remote debugging endpoint |

#### Excluding files

Use `--exclude GLOB` to skip files you do not want to download. Repeat the flag for more patterns; a file is skipped when any pattern matches it. Matching ignores case, so `*.mkv` also skips `Movie.MKV`.

Always quote the pattern. Unquoted, the shell expands `*.mkv` against your current directory before the tool sees it.

```sh
uv run main download-folder \
  --url "https://www.idrive.com/idrive/home/<device>_<device_id>/<drive>/path/to/folder" \
  --to "/media/<user>/<media>/<device>/path/to/folder" \
  --exclude '*crypt*' \
  --exclude '*.mkv'
```

A pattern is checked against one of two things, depending on whether it contains `/`:

- **No `/`:** it is matched against the file name only, in every folder of the download.
- **Contains `/`:** it is matched against the file's path inside the downloaded folder, the same path it gets under `--to`. The pattern starts at the top of the downloaded folder, as in `.gitignore`. `*` matches within a single folder name, and `**` matches any number of folders.

| Pattern | Skips | Keeps |
| ------- | ----- | ----- |
| `*.mkv` | `a.mkv`, `Videos/2020/b.MKV` | `a.mp4` |
| `*crypt*` | `notes.crypt`, `Docs/Encrypted.zip` | `notes.txt` |
| `Videos/*` | `Videos/a.mp4` | `Videos/2020/b.mp4`, `Old/Videos/c.mp4` |
| `Videos/**` | `Videos/a.mp4`, `Videos/2020/b.mp4` | `Old/Videos/c.mp4` |
| `**/Videos/**` | `Videos/a.mp4`, `Old/Videos/c.mp4` | `Old/c.mp4` |

Some `.gitignore` syntax is rejected with an error instead of being silently ignored, because it does not mean the same thing here:

- A leading `/`: patterns containing `/` already start at the top of the downloaded folder, so write `Videos/*` instead of `/Videos/*`.
- A trailing `/`: patterns match files, not folders, so write `Videos/**` instead of `Videos/`.
- A leading `!`: negation is not supported. To skip files whose names start with `!`, write `**/!draft*` instead of `!draft*`.

What happens to excluded files:

- The manifest lists each one as skipped, with the pattern that matched, and patterns used are stored in the manifest header. They are not part of the manifest's list of expected files, so `verify-manifest` does not report them as missing and `retry-manifest` does not download them.
- The final `Skipped:` count includes them together with files that already existed. The per-folder log line shows them separately, for example `3 file(s) already present, 2 excluded, 5 to download in ...`.
- Folders are still opened and listed during the crawl, even when a pattern like `Videos/**` skips all of their files.
- Manifests from runs before `--exclude` existed, or run without it, still expect those files, so `retry-manifest` on them downloads them. Rerun `download-folder` with `--exclude` instead.

### 4. Check for missing files (optional)

After a download run, verify that all expected files are present:

```sh
uv run main verify-manifest --manifest <path/to/manifest.json>
```

### 5. Retry missing files (optional)

Re-download only the files that are still missing:

```sh
uv run main retry-manifest --manifest <path/to/manifest.json> --headed
```

## Persistent browser session

The `browse` command keeps a Playwright-controlled Chromium browser running in the background, attached via Chrome DevTools Protocol on `http://127.0.0.1:9222`. This avoids reopening the browser and repeating the IDrive login or 2FA after every command.

Start or attach to the persistent session:

```sh
uv run main browse
```

The browser profile is stored under `.agents/playground/browser-state/idrive-chromium`. Once the session is open, `download-folder` and `retry-manifest` will attach to it automatically when `--headed` is set.

Use `--browser-debug-url <URL>` on any command to attach to a different Chromium remote debugging endpoint.

## All commands

```sh
uv run main auth                  # Log in and cache IDrive session
uv run main browse                # Open authenticated browser for manual navigation
uv run main download-folder       # Download all files in an IDrive folder
uv run main verify-manifest       # Check which files from a manifest are still missing
uv run main retry-manifest        # Re-download only the missing files
```

## Development

```sh
uv sync                   # Install or refresh dependencies
uv run poe test           # Run tests
uv run poe lint           # Check formatting
uv run poe lint-fix       # Auto-format code
uv run poe typecheck      # Run Pyright strict mode
```

## Monitoring resource usage

For long-running downloads, watch the memory, disk I/O, and GPU usage of the download process and the browser it drives:

```sh
uv run python scripts/monitor-resource-usage.py --interval 30 --out .agents/playground --to-destination "/media/me/backup/device-2"
```

Pass `--to-destination` the **same path you give the download `--to` flag** (e.g. `"D:\Backups"` on Windows); its volume's free space is reported. `--out` is a directory; the script writes one timestamped `usage-<start>.csv` per run into it (`.agents/playground` is the ignored scratch folder). disk busy% and read/write rates already cover all disks regardless. It prints a line to the console each interval and appends the full metrics to the CSV. Works on Windows and Linux; stop with Ctrl-C.

## Legacy scripts

These scripts predate the `main` CLI and are kept for reference.

### `scripts/deepFolderExtractor.js`

A browser-side script that crawls the IDrive web interface and exports a JSON list of folders.

1. Open the IDrive web page for the device and volume you want to inspect.
2. Open the browser developer console (`F12` → `Console`, or `Ctrl+Shift+I` → `Console`).
3. Paste the contents of `scripts/deepFolderExtractor.js` and press Enter.
4. Use the panel that appears in the bottom-right corner. Set a `Search Depth` and click `Start Crawl`.
5. Your browser downloads a `.json` file when the crawl finishes.

Depth `0` lists only the folders on the current screen. Depth `1` goes one level deeper. Higher values take longer.

### `scripts/backup_util.py`

Reads the JSON file exported by `deepFolderExtractor.js` and asks the IDrive Linux application to restore the matching folders.

```sh
uv run poe restore my_export.json --email your-name@example.com --depth 1
```

Restored files are placed in IDrive's normal restore location:

```text
/opt/IDriveForLinux/idriveIt/user_profile/<your-linux-user>/<your-email>/Restore/DefaultRestoreSet/RestoreData
```

Requires [IDrive for Linux](https://www.idrive.com/online-backup-linux-scripts) installed and signed in.
