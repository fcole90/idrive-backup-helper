# IDRIVE Backup Tool

This project automates restoration of folders from IDrive based on a JSON payload extracted from the web interface. It exports folders from the browser, filters them by depth, and triggers the IDrive Linux restore engine for each matching path.

## Before you start

Before continuing, make sure you have:

- IDrive for Linux installed. If you still need it, download it from [IDrive Linux scripts](https://www.idrive.com/online-backup-linux-scripts).
- Signed in to IDrive for Linux at least once on the same machine.


## Open a terminal and extract the ZIP

- On Linux, open your applications menu and search for `Terminal`.
- On Windows, use [WSL](https://learn.microsoft.com/windows/wsl/install) if you want to follow these instructions exactly. WSL stands for Windows Subsystem for Linux and gives you a Linux terminal window on Windows. After WSL is installed, open the Start menu, search for `WSL` or `Ubuntu`, and open it.

If you downloaded this project as a ZIP file from GitHub, the file is often named something like `idrive-backup-helper-main.zip`.

For the examples below, assume the ZIP file, the extracted project folder, and the downloaded JSON file are all in your `Downloads` folder.

`Downloads` is only an example. If your files are somewhere else, use that folder instead.

You can extract the ZIP in `Downloads` and rename the extracted folder to `idrive-backup-helper` with:

```sh
cd ~/Downloads
unzip idrive-backup-helper-main.zip
mv idrive-backup-helper-main idrive-backup-helper
```

## Set up uv

Install `uv`:

- macOS/Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Windows PowerShell: `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`

Then install the project environment from the repository root:

```sh
cd ~/Downloads/idrive-backup-helper
uv sync
```

This project targets Python `3.14`, and for the normal setup documented here `uv sync` will take care of creating the local environment you need.

After `uv sync`, you may want to close and reopen your terminal or IDE so the local virtual environment is picked up cleanly.

## Workflow overview

You will use this project in two steps:

1. Run `scripts/deepFolderExtractor.js` in your browser to download a JSON file that lists folders from IDrive.
2. Run `uv run poe restore` in a terminal to restore folders from that JSON file.

You will mainly use these files from the `scripts/` folder:

- `scripts/deepFolderExtractor.js` exports a list of folders from the IDrive website.
- `scripts/backup_util.py` reads that exported JSON file and asks the IDrive Linux application to restore the matching folders.

## Export folders with `scripts/deepFolderExtractor.js`

This script runs inside your web browser, not in the terminal. Its job is to visit the folders visible in the IDrive web interface, scroll until everything is loaded, and download a JSON file that lists the folders it found.

### What the extractor does

It adds a small panel to the page with a `Search Depth` box and a `Start Crawl` button.

- Depth `0` means only the folders in the current screen.
- Depth `1` means the current screen plus one level deeper.
- Higher numbers go deeper, but they can take much longer.

For most people, depth `1` is a good starting point.

### How to use it in the browser

1. Open the IDrive web page for the device and volume you want to inspect.
2. Open the browser developer console.
3. Paste the contents of `scripts/deepFolderExtractor.js` into that console.
4. Press Enter on your keyboard. That makes the browser execute the pasted script.
5. Use the panel that appears in the bottom-right corner of the page.

To open the console in most browsers:

- On Linux, press `F12`, then choose the `Console` tab.
- If `F12` does not work, try `Ctrl+Shift+I`, then choose `Console`.

To paste the script:

1. Open `scripts/deepFolderExtractor.js` in a text editor.
2. Select all of its contents.
3. Copy it.
4. Click inside the browser console.
5. Paste the copied text.
6. Press Enter. That tells the browser to execute the pasted text.

After the crawl finishes, your browser should download a `.json` file. Keep that file, because the restore command needs it.

In the examples below, `my_export.json` is only a sample file name. Your downloaded file may have a different name.

## Restore folders with `uv run poe restore`

The recommended way to run the restore flow is through the Poe task wrapper:

```sh
uv run poe restore my_export.json --email your-name@example.com --depth 1
```

That command runs `scripts/backup_util.py` inside the project's managed environment.

### What it needs

- The same email address you use with IDrive for Linux
- The JSON file downloaded from the browser

### What it does

For each matching folder, the script updates IDrive's restore list and runs the IDrive restore command automatically. Restored files are placed in IDrive's normal restore location on your system.

### How to use it

1. Open a terminal.
2. Change into the extracted project folder.
3. Make sure your downloaded JSON file is in that folder, or use the full path to the file instead.
4. Run the command shown below, replacing the example email address with your own.

Example:

```sh
cd ~/Downloads/idrive-backup-helper
uv sync
uv run poe restore my_export.json --email your-name@example.com --depth 1
```

If you want to see the available options:

```sh
uv run poe restore --help
```

### Understanding the depth value

The `--depth` value tells the script which folder level to restore from the exported list.

- `--depth 1` restores first-level folders from the chosen starting point.
- `--depth 2` restores the next level down.
- If you omit `--depth`, the script will try every folder in the JSON file.

If you are unsure, start with a smaller depth so you can confirm the results before restoring a larger set.

### Where restored data goes

When the script finishes, it prints the restore destination used by IDrive. In this setup, restored data is expected under:

```text
/opt/IDriveForLinux/idriveIt/user_profile/<your-linux-user>/<your-email>/Restore/DefaultRestoreSet/RestoreData
```

## Development

Use the managed environment for local development:

```sh
uv sync
```

Run the main validation commands with Poe:

```sh
uv run poe test
uv run poe lint
uv run poe typecheck
```

If you work on the local Copilot skill and policy setup, these repository tasks are also available:

```sh
uv run poe sync-skills
uv run poe sync-ai-policy
uv run poe policy-check
```

## Persistent browser session for web downloads

The `main` CLI can keep a Playwright-controlled Chromium browser detached from each command run. This avoids reopening the browser and repeating IDrive login/2FA after every script crash or code change.

Open or attach to the persistent browser session with:

```sh
uv run main browse
```

The command uses `http://127.0.0.1:9222` by default and stores the Chromium profile under `.agents/playground/browser-state/idrive-chromium`. The command returns after opening the page; the browser process stays open.

Then run headed downloads against the same browser session:

```sh
uv run main download-folder --headed --url <IDRIVE_FOLDER_URL> --to <DESTINATION>
```

Use `--browser-debug-url <URL>` on `browse`, `download-folder`, or `retry-manifest` when you want to attach to a different Chromium remote debugging endpoint.
