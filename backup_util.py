"""
Mr. Wolf's IDrive Batch Executor

This script is designed to automate the restoration of folders from IDrive based on a JSON payload extracted from the web interface. 
It reads the folder structure, filters by specified depth, and triggers the IDrive restore engine for each target folder.

Check the README.md for usage instructions and the deepFolderExtractor.js script for how to generate the required JSON payload from the IDrive web interface.

A following pCloud integration can be added to this script to automatically move the restored folders to a pCloud account, 
but for now it focuses solely on the IDrive restoration process.

"""

#!/usr/bin/env python3
import json
import subprocess
import os
import sys
import urllib.parse
import argparse

# --- 1. ARGUMENT PARSING ---
parser = argparse.ArgumentParser(description="Wolf IDrive Batch Executor")
parser.add_argument("payload", help="Path to the downloaded JSON payload file")
parser.add_argument("--email", "-e", type=str, help="Email address associated with the IDrive account")
parser.add_argument("-d", "--depth", type=int, default=None, 
                    help="Target folder depth to process (0 = root, 1 = first-level subfolders, etc.)")
args = parser.parse_args()

JSON_PAYLOAD_FILE = args.payload
TARGET_DEPTH = args.depth
EMAIL = args.email
CURRENT_USER_NAME = os.getlogin()

# --- 2. CONFIGURATION ---
IDRIVE_BIN_DIR = "/opt/IDriveForLinux/bin"
IDRIVE_BASE_DATA_DIR = "/opt/IDriveForLinux/idriveIt/user_profile"
IDRIVE_USER_DIR = os.path.join(IDRIVE_BASE_DATA_DIR, CURRENT_USER_NAME, EMAIL)
ONLINE_RESTORE_DIR = os.path.join(IDRIVE_USER_DIR, "Restore", "DefaultRestoreSet")
ONLINE_RESTORE__DATA_DIR = os.path.join(ONLINE_RESTORE_DIR, "RestoreData")
RESTORE_SET_FILE = os.path.join(ONLINE_RESTORE_DIR, "RestoresetFile.txt")


# --- 3. PATH RESOLUTION ENGINE ---
def get_folder_path(href):
    if not href:
        return ""
    
    raw_path = href.replace("https://www.idrive.com/idrive/home/", "")
    split_path = raw_path.split('/')
    print(f"Raw path: {raw_path}, Split path: {split_path}")
    if len(split_path) < 2:
        raise ValueError(f"Unexpected path format: {split_path}. href: {href}")
    
    # Remove the device name
    path = os.path.join(*split_path[1:])

    clean_path = urllib.parse.unquote(path)
    
    if not clean_path.startswith('/'):
        clean_path = '/' + clean_path
        
    return clean_path

# --- 4. PRE-FLIGHT CHECKS ---
if not os.path.isdir(IDRIVE_BIN_DIR):
    print(f"ERROR: IDrive directory not found at {IDRIVE_BIN_DIR}")
    sys.exit(1)

if not os.path.isdir(IDRIVE_USER_DIR):
    print(f"ERROR: User directory not found at {IDRIVE_USER_DIR}. Check if the email argument is correct and if you have run IDrive at least once.")
    sys.exit(1)

if not os.path.isfile(JSON_PAYLOAD_FILE):
    print(f"ERROR: JSON payload not found at {JSON_PAYLOAD_FILE}")
    sys.exit(1)

# --- 5. LOAD & FILTER PAYLOAD ---
try:
    with open(JSON_PAYLOAD_FILE, 'r') as f:
        data = json.load(f)
        raw_directories = data.get("dirs", [])
except Exception as e:
    print(f"ERROR: Failed to parse JSON file. {e}")
    sys.exit(1)

# Filter the execution queue based on the requested depth
directories = []
for item in raw_directories:
    item_depth = item.get("depth")
    
    # If a specific depth was requested, enforce the filter
    if TARGET_DEPTH is not None:
        if item_depth == TARGET_DEPTH:
            directories.append(item)
    else:
        # If no depth specified, add everything (Warning: Potential overlaps)
        directories.append(item)

if not directories:
    depth_msg = TARGET_DEPTH if TARGET_DEPTH is not None else "All"
    print(f"WARNING: No directories found matching the requested criteria (Depth: {depth_msg}).")
    sys.exit(0)

print(f"Queue verified. Found {len(directories)} directories at depth {TARGET_DEPTH if TARGET_DEPTH is not None else 'ALL'}.")

# --- 6. EXECUTION LOOP ---
for index, item in enumerate(directories, start=1):
    folder_href = item.get("href")
    
    absolute_target_path = get_folder_path(folder_href)
    
    if not absolute_target_path or absolute_target_path == '/':
        continue

    print("=" * 50)
    print(f"TARGET ACQUIRED [{index}/{len(directories)}]: {absolute_target_path}")
    print("=" * 50)

    # Overwrite the RestoreList.txt file with the absolute target
    os.makedirs(os.path.dirname(RESTORE_SET_FILE), exist_ok=True)

    # Remove all files in the dir that are not dirs themselves to prevent conflicts with the IDrive engine
    for filename in os.listdir(ONLINE_RESTORE_DIR):
        file_path = os.path.join(ONLINE_RESTORE_DIR, filename)
        if os.path.isfile(file_path):
            try:
                os.remove(file_path)
                print(f"Removed file: {file_path}")
            except Exception as e:
                print(f"WARNING: Could not remove file {file_path}. {e}")

    try:
        with open(RESTORE_SET_FILE, 'w') as f:
            f.write(absolute_target_path + '\n')
    except IOError as e:
        print(f"ERROR: Could not write to {RESTORE_SET_FILE}. {e}")
        sys.exit(1)


    # Trigger the IDrive engine non-interactively
    print("Initiating IDrive restore engine...")
    try:
        result = subprocess.run(
            ['./idrive', '--restore'], 
            cwd=IDRIVE_BIN_DIR,
            check=False
        )
        
        if result.returncode == 0:
            print(f"COMPLETED: {absolute_target_path}\n")
        else:
            print(f"WARNING: IDrive exited with code {result.returncode} for {absolute_target_path}\n")
            
    except Exception as e:
        print(f"FATAL ERROR executing IDrive: {e}")
        sys.exit(1)

print("All specified folders for the selected depth have been processed.")
print(f"You can find them at {ONLINE_RESTORE__DATA_DIR}.")
